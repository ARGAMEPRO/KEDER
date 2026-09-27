"""КЕДР API: JWT auth, detection, escalation, weather/hazard/evacuation, WebSockets, PDF."""
import asyncio
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from shapely.geometry import Point, mapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import services as sv
from .models import (GpsSource, init_db, Incident, Role, SessionLocal, Settlement, Source, Status, User,
                     TIER_PENDING, classify, get_db, utcnow)
from .seed import pwd, seed

SECRET = os.getenv("SECRET_KEY", "dev-secret-change-me")
FPS = float(os.getenv("FRAME_SAMPLE_FPS", 1))
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", 60))
UPLOADS = Path(os.getenv("UPLOAD_DIR", "uploads"))
for sub in ("images", "false_positives"):
    (UPLOADS / sub).mkdir(parents=True, exist_ok=True)


PROD = os.getenv("APP_ENV", "dev").strip().lower() in ("prod", "production")


def ensure_weights():
    """Optional: fetch detection weights on first boot when YOLO_WEIGHTS_URL is set."""
    path, url = os.getenv("YOLO_WEIGHTS", "weights/yolov8x-fire.pt"), os.getenv("YOLO_WEIGHTS_URL", "")
    if not url or os.path.exists(path):
        return
    import httpx
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r, open(path + ".part", "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            f.write(chunk)
    os.replace(path + ".part", path)


@asynccontextmanager
async def lifespan(_):
    if PROD and SECRET == "dev-secret-change-me":
        raise RuntimeError("Set a strong SECRET_KEY when APP_ENV=production")
    if os.getenv("SEED_ON_STARTUP", "false" if PROD else "true").lower() == "true":
        seed()
    else:
        init_db()
    try:
        await run_in_threadpool(ensure_weights)
    except Exception as e:
        print(f"[kedr] weights download failed: {e}", flush=True)
    yield


app = FastAPI(title="КЕДР API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.getenv("CORS_ORIGINS", "").split(",") if o],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
api = APIRouter(prefix="/api/v1")
oauth = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)
_hits = defaultdict(deque)


def limiter(per_minute: int):
    """Tiny in-memory per-IP rate limit (single-process); use a gateway/Redis if you scale out."""
    def dep(request: Request):
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        key = (request.url.path, fwd or (request.client.host if request.client else "?"))
        q, now = _hits[key], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            raise HTTPException(429, "Слишком много запросов, попробуйте через минуту")
        q.append(now)
    return dep


# ---------- auth ----------
def decode(token):
    try:
        return int(jwt.decode(token, SECRET, algorithms=["HS256"])["sub"])
    except (JWTError, KeyError, ValueError):
        return None


def current_user(token: str | None = Depends(oauth), db: Session = Depends(get_db)):
    if not token:
        return None
    uid = decode(token)
    if uid is None:
        raise HTTPException(401, "Invalid token")
    return db.get(User, uid)


def require(*roles):
    def dep(u: User | None = Depends(current_user)):
        if not u or u.role not in roles:
            raise HTTPException(403, "Forbidden")
        return u
    return dep


class Login(BaseModel):
    email: str
    password: str


class Register(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(default="", max_length=255)


def _validate_password(p: str):
    if not re.search(r"[A-Za-zА-Яа-я]", p) or not re.search(r"\d", p):
        raise HTTPException(422, "Пароль: минимум 8 символов, буква и цифра")


def user_out(u):
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role.value}


def _issue(u: User):
    exp = datetime.now(timezone.utc) + timedelta(minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 720)))
    return {"access_token": jwt.encode({"sub": str(u.id), "exp": exp}, SECRET, algorithm="HS256"),
            "token_type": "bearer", "user": user_out(u)}


@api.post("/auth/login", dependencies=[Depends(limiter(10))])
def login(body: Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not pwd.verify(body.password, u.hashed_password):
        raise HTTPException(401, "Wrong email or password")
    return _issue(u)


@api.post("/auth/register", dependencies=[Depends(limiter(8))])
def register(body: Register, db: Session = Depends(get_db)):
    """Open self-service registration (item 3): anyone becomes a CITIZEN and is logged in at once.

    Roles are never accepted from the client — staff accounts are seeded/assigned server-side only.
    """
    email = body.email.strip().lower()
    if len(email) > 255 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(422, "Некорректный адрес почты")
    _validate_password(body.password)
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Пользователь с такой почтой уже есть")
    u = User(email=email, full_name=body.full_name.strip()[:255], role=Role.CITIZEN,
             hashed_password=pwd.hash(body.password))
    db.add(u)
    try:
        db.commit()
    except IntegrityError:  # concurrent signup with the same address
        db.rollback()
        raise HTTPException(409, "Пользователь с такой почтой уже есть")
    db.refresh(u)
    return _issue(u)


@api.get("/auth/me")
def me(u: User | None = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    return user_out(u)


# ---------- websocket hub ----------
class Hub:
    def __init__(self):
        self.clients = []

    async def send(self, msg, roles):
        for ws, role in list(self.clients):
            if role in roles:
                try:
                    await ws.send_json(jsonable_encoder(msg))
                except Exception:
                    try:
                        self.clients.remove((ws, role))
                    except ValueError:
                        pass


hub = Hub()


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket, token: str = ""):
    uid = decode(token)
    with SessionLocal() as db:
        u = db.get(User, uid) if uid else None
    if not u or u.role not in (Role.FORESTER, Role.MCHS):
        await ws.close(code=4401)
        return
    await ws.accept()
    entry = (ws, u.role)
    hub.clients.append(entry)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if entry in hub.clients:
            hub.clients.remove(entry)


# ---------- incidents ----------
def out(i: Incident):
    return {c.name: getattr(i, c.name) for c in i.__table__.columns}


async def enrich(db: Session, i: Incident):
    """Weather + region + OSM context in parallel; picks the fuel zone and builds the spread polygon."""
    (wx, _err), (region, cc), near, (facilities, _) = await asyncio.gather(
        sv.weather(i.lat, i.lon), sv.reverse_geocode(i.lat, i.lon), sv.nearby(i.lat, i.lon),
        sv.infrastructure_near(i.lat, i.lon, 15000, 50000))
    if wx:
        i.wind_speed_ms, i.wind_dir_deg, i.temp_c, i.humidity_pct = wx["wind_speed_ms"], wx["wind_dir_deg"], wx["temp_c"], wx["humidity_pct"]
    else:
        i.wind_speed_ms = i.wind_dir_deg = i.temp_c = i.humidity_pct = None
    i.weather_fallback = 0 if wx else 1  # 1 = weather unavailable (never filled with fake values)
    i.region_name = region
    if not i.region_name and i.lat is not None:
        # Nominatim молчит → берём ближайший известный город из локальной БД
        candidates = db.query(Settlement).all()
        if candidates:
            nearest = min(candidates, key=lambda s: sv.haversine_km(i.lat, i.lon, s.lat, s.lon))
            d = sv.haversine_km(i.lat, i.lon, nearest.lat, nearest.lon)
            if d <= 50:
                i.region_name = f"{nearest.region}, {nearest.name} (~{d:.0f} км)"
    if cc and cc != "ru":
        raise HTTPException(422, "Точка находится вне России")
    fuel = sv.classify_fuel(i.lat, i.lon, near)
# Если Overpass не ответил — классифицируем по локальной БД городов
    if not near.get("landuse") and not near.get("places"):
        big = [s for s in db.query(Settlement).filter(Settlement.population >= 20000).all()
            if sv.haversine_km(i.lat, i.lon, s.lat, s.lon) <= 15]
        if big:
            fuel = "urban"
        else:
            near_any = [s for s in db.query(Settlement).all()
                        if sv.haversine_km(i.lat, i.lon, s.lat, s.lon) <= 30]
            fuel = "mixed" if near_any else "forest"
    print(f"[kedr] enrich: fuel type = {fuel}, places from OSM = {len(near.get('places', []))}")
    # Calculate additional fire spread metrics
    burn_minutes = sv._FUEL.get(fuel, sv._FUEL["forest"])["burn"]
    ros = sv.rothermel_ros(fuel, i.wind_speed_ms, None, i.humidity_pct, i.temp_c)
    p_burn = sv.cellular_automata_pburn(fuel, i.wind_speed_ms, i.wind_dir_deg, None)

    # Get terrain type using osmnx if available
    terrain_type = sv.get_terrain_type(i.lat, i.lon)
    print(f"[kedr] enrich: terrain type = {terrain_type}")

    # Calculate hazard polygon with cell-level data
    poly, cells_data = sv.hazard_polygon(i.lat, i.lon, i.wind_speed_ms, i.wind_dir_deg, i.temp_c, i.humidity_pct, fuel=fuel, burn_minutes=burn_minutes)

    # Check for fire barriers (roads, rivers)
    radius_m = ros * burn_minutes  # approximate spread radius in meters (ros is already in m/min)
    barriers = sv.check_fire_barriers(i.lat, i.lon, radius_m, poly)
    print(f"[kedr] enrich: found {len(barriers)} fire barriers")
    places_osm = near.get("places") or []
    src = near.get("source", "none")

    # 1) settlements inside fire polygon + 3km buffer
    selected, picked = sv.select_places(poly, places_osm, i.lat, i.lon, extra_km=30)

    # 2) if polygon didn't cover anyone (or OSM empty) — take nearest within 30km
    if not selected:
        # try local seed
        local = [
            {"name": s.name, "lat": s.lat, "lon": s.lon,
             "type": "city", "population": s.population}
            for s in db.query(Settlement).all()
            if sv.haversine_km(i.lat, i.lon, s.lat, s.lon) <= 30
        ]
        if local:
            selected, picked, src = local, "local", "local"
        else:
            # last resort — take nearest OSM within 30km, even if outside polygon
            near30 = sorted(
                places_osm,
                key=lambda p: sv.haversine_km(i.lat, i.lon, p["lat"], p["lon"]),
            )[:10]
            selected, picked = near30, "nearby"

    i.evacuation = sv.evacuation_plan(poly, i.lat, i.lon, selected, facilities, i.wind_dir_deg)

    # Calculate max criticality from facilities
    max_crit = max((f["criticality"] for f in facilities), default=0)

    i.hazard_geojson = {"type": "Feature", "geometry": mapping(poly),
                        "properties": {
                            "mode": "wind" if wx else "circle",
                            "settlements": src,
                            "picked": picked,
                            "fuel": fuel,
                            "ros_m_per_min": round(ros, 2),
                            "p_burn": round(p_burn, 3),
                            "overpass_available": src != "local",
                            "terrain": terrain_type,
                            "cells": cells_data[::max(len(cells_data) // 100, 1)][:100] if cells_data else [],
                            "barriers": barriers,
                            "barrier_count": len(barriers),
                            "max_criticality": max_crit,
                            "facilities_count": len(facilities)
                        }}


async def notify(i: Incident):
    if i.status == Status.PENDING_VERIFICATION:
        await hub.send({"type": "PENDING_VERIFICATION", "incident": out(i)}, {Role.FORESTER, Role.MCHS})
    elif i.status in (Status.CRITICAL_ALERT, Status.CONFIRMED):
        await hub.send({"type": "CRITICAL_ALERT", "incident": out(i)}, {Role.MCHS})


def fold_clear(db: Session, i: Incident) -> Incident | None:
    """CLEAR rows are near-misses: fold repeats from the same spot into the first row."""
    tol = float(os.getenv("CLEAR_DEDUPE_KM", 0.5))
    if i.lat is None or i.lon is None:
        return None
    rows = (db.query(Incident).filter(Incident.id != i.id, Incident.status == Status.CLEAR,
                                      Incident.lat.isnot(None), Incident.lon.isnot(None)).all())
    for r in rows:
        if sv.haversine_km(i.lat, i.lon, r.lat, r.lon) <= tol:
            r.confidence = max(r.confidence, i.confidence)
            r.detections = (r.detections or []) + (i.detections or [])
            r.created_at = utcnow()
            db.delete(i)
            return r
    return None


async def create_from_analysis(db, res, source, lat, lon, gps, user):
    conf, dets, raw, ann = res
    i = Incident(source=source, confidence=conf, status=classify(conf), detections=dets,
                 lat=lat, lon=lon, gps_source=gps, reporter_id=user.id if user else None)
    db.add(i)
    db.flush()
    if i.status == Status.CLEAR and lat is not None:
        await enrich(db, i)
        dup = fold_clear(db, i)  # may delete `i`; its frames were never written yet
        db.commit()
        if dup is not None:
            return dup
    stem = f"{i.id}_{uuid.uuid4().hex[:8]}"
    cv2.imwrite(str(UPLOADS / "images" / f"{stem}.jpg"), raw)
    cv2.imwrite(str(UPLOADS / "images" / f"{stem}_ann.jpg"), ann)
    i.image_path, i.annotated_path = f"images/{stem}.jpg", f"images/{stem}_ann.jpg"
    if i.status != Status.CLEAR and lat is not None:
        await enrich(db, i)
    db.commit()
    await notify(i)
    return i


async def stream_upload(file: UploadFile) -> str:
    """Stream an upload to a temp file, enforcing MAX_UPLOAD_MB. Returns the temp path."""
    limit = int(os.getenv("MAX_UPLOAD_MB", 100)) << 20
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "f").suffix, delete=False) as tmp:
        size = 0
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                tmp.close()
                os.unlink(tmp.name)
                raise HTTPException(413, f"Файл больше {limit >> 20} МБ")
            tmp.write(chunk)
        return tmp.name


@api.post("/incidents/report", dependencies=[Depends(limiter(int(os.getenv("RATE_LIMIT_PER_MIN", 10))))])
async def report(file: UploadFile = File(...), lat: float | None = Form(None), lon: float | None = Form(None),
                 location_note: str | None = Form(None),
                 db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ctype = file.content_type or ""
    # Support various video formats (including browser-specific MIME types)
    video_formats = ["video/mp4", "video/avi", "video/mov", "video/mkv", "video/wmv", "video/flv", "video/webm", "video/quicktime",
                     "video/x-matroska", "video/x-msvideo", "video/x-ms-wmv", "video/vnd.avi"]
    image_formats = ["image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "image/tiff"]

    if not (ctype.startswith("image/") or any(ctype.startswith(fmt) for fmt in video_formats)):
        raise HTTPException(415, "Допустимы только фото и видео (MP4, AVI, MOV, MKV, WMV, FLV, WebM)")

    is_video = any(ctype.startswith(fmt) for fmt in video_formats)
    tmp_name = await stream_upload(file)
    gps = GpsSource.DEVICE if lat is not None and lon is not None else GpsSource.NONE
    try:
        if not is_video and (exif := sv.exif_gps(tmp_name)):
            (lat, lon), gps = exif, GpsSource.EXIF
        if lat is not None and lon is not None and not in_russia(lat, lon):
            lat = lon = None
            gps = GpsSource.NONE
        max_frames = int(MAX_VIDEO_SECONDS * FPS) if is_video else None
        res = await run_in_threadpool(sv.analyze, tmp_name, is_video, FPS, max_frames)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    finally:
        os.unlink(tmp_name)
    if not res:
        raise HTTPException(422, "Could not read the uploaded file")
    i = await create_from_analysis(db, res, Source.VIDEO if is_video else Source.PHOTO, lat, lon, gps, user)
    if location_note and location_note.strip():
        i.region_name = (i.region_name + " · " if i.region_name else "") + location_note.strip()[:200]
        db.commit()
    return out(i)


class Rtsp(BaseModel):
    url: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    seconds: int = Field(10, ge=1, le=120)


@api.post("/incidents/rtsp")
async def scan_rtsp(body: Rtsp, db: Session = Depends(get_db), user: User = Depends(require(Role.MCHS))):
    try:
        res = await run_in_threadpool(sv.analyze, body.url, True, FPS, body.seconds)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    if not res:
        raise HTTPException(422, "No frames received from stream")
    return out(await create_from_analysis(db, res, Source.RTSP, body.lat, body.lon, GpsSource.MANUAL, user))


class Manual(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


@api.post("/incidents/manual")
async def manual(body: Manual, db: Session = Depends(get_db), user: User = Depends(require(Role.MCHS))):
    """MCHS manual entry without a photo = dispatcher confirmation (item 5)."""
    check_bbox(body.lat, body.lon)
    i = Incident(source=Source.MANUAL, status=Status.CONFIRMED, confidence=1.0, lat=body.lat, lon=body.lon,
                 gps_source=GpsSource.MANUAL, reporter_id=user.id, detections=[])
    db.add(i)
    await enrich(db, i)
    db.commit()
    await notify(i)
    return out(i)


@api.post("/incidents/manual-photo")
async def manual_photo(
    file: UploadFile | None = File(None),
    lat: float = Form(...),
    lon: float = Form(...),
    verdict: str = Form("auto"),
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.FORESTER, Role.MCHS)),
):
    """Forester: photo required, AI confidence still drives escalation.

    MCHS: photo optional; `verdict=critical` raises a critical alert, otherwise the dispatcher
    confirmation wins even when the network is unsure (item 5).
    """
    check_bbox(lat, lon)
    if user.role == Role.FORESTER and file is None:
        raise HTTPException(422, "Лесничему нужно приложить фото очага")
    conf, dets, image_path, ann_path = None, [], "", ""
    if file is not None:
        ctype = file.content_type or ""
        if not ctype.startswith("image/"):
            raise HTTPException(415, "Нужно фото для подтверждения")
        tmp_name = await stream_upload(file)
        try:
            res = await run_in_threadpool(sv.analyze, tmp_name, False, FPS)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        finally:
            os.unlink(tmp_name)
        if not res:
            raise HTTPException(422, "Не удалось прочитать фото")
        conf, dets, raw, ann = res
        if user.role == Role.FORESTER and conf < TIER_PENDING:
            raise HTTPException(422, "На фото не обнаружен огонь/дым — загрузите снимок очага")
    if user.role == Role.MCHS:
        status = Status.CRITICAL_ALERT if verdict == "critical" else Status.CONFIRMED
        confidence = max(conf or 0.0, 0.5)
    else:
        status, confidence = classify(conf), conf
    i = Incident(source=Source.MANUAL, confidence=confidence, status=status, lat=lat, lon=lon,
                 gps_source=GpsSource.MANUAL, reporter_id=user.id, detections=dets)
    db.add(i)
    db.flush()
    if file is not None:
        stem = f"{i.id}_{uuid.uuid4().hex[:8]}"
        cv2.imwrite(str(UPLOADS / "images" / f"{stem}.jpg"), raw)
        cv2.imwrite(str(UPLOADS / "images" / f"{stem}_ann.jpg"), ann)
        i.image_path, i.annotated_path = f"images/{stem}.jpg", f"images/{stem}_ann.jpg"
    await enrich(db, i)
    db.commit()
    await notify(i)
    return out(i)


@api.get("/incidents/public")
def public_incidents(db: Session = Depends(get_db)):
    rows = (db.query(Incident).filter(Incident.status.in_([Status.CONFIRMED, Status.CRITICAL_ALERT]),
                                      Incident.lat.isnot(None)).order_by(Incident.created_at.desc()).limit(200))
    return [{k: getattr(r, k) for k in ("id", "lat", "lon", "status", "confidence", "created_at", "region_name")}
            for r in rows]


@api.get("/incidents")
def list_incidents(status: str | None = None, include_clear: int = 0, db: Session = Depends(get_db),
                   user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """Staff feed. By default every status except CLEAR; `include_clear=1` also returns near-misses."""
    q = db.query(Incident)
    if status:
        q = q.filter(Incident.status == Status(status))
    elif not include_clear:
        q = q.filter(Incident.status != Status.CLEAR)
    return [out(i) for i in q.order_by(Incident.created_at.desc()).limit(300)]


class LocationPatch(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    note: str | None = Field(default=None, max_length=200)


@api.patch("/incidents/{iid}/location")
async def patch_location(iid: int, body: LocationPatch, db: Session = Depends(get_db),
                         user: User = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    """Item 1: refine the point by hand — provenance is recorded as MANUAL and the zone is recomputed."""
    i = db.get(Incident, iid)
    if not i:
        raise HTTPException(404, "Not found")
    if user.role == Role.CITIZEN and i.reporter_id != user.id:
        raise HTTPException(403, "Forbidden")
    check_bbox(body.lat, body.lon)
    i.lat, i.lon, i.gps_source = body.lat, body.lon, GpsSource.MANUAL
    if body.note and body.note.strip():
        i.region_name = (i.region_name.split(" · ")[0] + " · " if i.region_name else "") + body.note.strip()[:200]
    if i.status != Status.CLEAR:
        await enrich(db, i)
    db.commit()
    return out(i)


@api.delete("/incidents/{iid}")
async def delete_incident(iid: int, db: Session = Depends(get_db),
                          user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    i = db.get(Incident, iid)
    if not i:
        raise HTTPException(404, "Not found")
    for rel in (i.image_path, i.annotated_path):
        if rel:
            p = UPLOADS / rel
            if p.exists() and p.is_file():
                p.unlink()
    db.delete(i)
    db.commit()
    await hub.send({"type": "INCIDENT_DELETED", "id": iid}, {Role.FORESTER, Role.MCHS})
    return {"ok": True}


class Review(BaseModel):
    verdict: str  # "confirm" | "false_alarm"


@api.post("/incidents/{iid}/review")
async def review(iid: int, body: Review, db: Session = Depends(get_db),
                 user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """Human decision on a PENDING_VERIFICATION case — disponible to the forester and to MCHS (item 5)."""
    i = db.get(Incident, iid)
    if not i:
        raise HTTPException(404, "Not found")
    if i.status not in (Status.PENDING_VERIFICATION, Status.CRITICAL_ALERT):
        raise HTTPException(409, "Инцидент не находится на рассмотрении")
    if body.verdict == "false_alarm":
        i.status = Status.FALSE_ALARM
        src = UPLOADS / i.image_path
        if i.image_path and src.exists():  # clean image becomes a hard negative for retraining
            shutil.move(src, UPLOADS / "false_positives" / src.name)
            i.image_path = f"false_positives/{src.name}"
    elif body.verdict == "confirm":
        i.status = Status.CONFIRMED
        if i.hazard_geojson is None and i.lat is not None:
            await enrich(db, i)
        # Refresh hazard_geojson if it exists but doesn't have cells data
        elif i.hazard_geojson and isinstance(i.hazard_geojson, dict):
            props = i.hazard_geojson.get("properties", {})
            if "cells" not in props:
                await enrich(db, i)
    else:
        raise HTTPException(422, "verdict must be confirm or false_alarm")
    i.reviewed_by_id, i.reviewed_at = user.id, utcnow()
    db.commit()
    if i.status == Status.CONFIRMED:
        await hub.send({"type": "CRITICAL_ALERT", "incident": out(i)}, {Role.MCHS})
    return out(i)


@api.get("/incidents/{iid}/report.pdf")
def report_pdf(iid: int, db: Session = Depends(get_db), user: User = Depends(require(Role.MCHS))):
    i = db.get(Incident, iid)
    if not i or i.lat is None:
        raise HTTPException(404, "Incident with GPS not found")
    return Response(sv.make_pdf(i, UPLOADS), media_type="application/pdf")


@api.get("/incidents/general-report")
def general_report(db: Session = Depends(get_db), user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """General report for all incidents."""
    incidents = db.query(Incident).order_by(Incident.created_at.desc()).limit(500).all()
    report_data = {
        "total": len(incidents),
        "by_status": {},
        "by_fuel": {},
        "recent": []
    }

    for i in incidents:
        status = i.status.value
        report_data["by_status"][status] = report_data["by_status"].get(status, 0) + 1

        if i.hazard_geojson and isinstance(i.hazard_geojson, dict):
            fuel = i.hazard_geojson.get("properties", {}).get("fuel", "unknown")
            report_data["by_fuel"][fuel] = report_data["by_fuel"].get(fuel, 0) + 1

        report_data["recent"].append({
            "id": i.id,
            "status": status,
            "confidence": i.confidence,
            "lat": i.lat,
            "lon": i.lon,
            "region": i.region_name,
            "created_at": (i.created_at or utcnow()).isoformat(),
            "evacuation_count": len(i.evacuation) if i.evacuation else 0
        })

    return report_data


@api.get("/health")
def health():
    return {"ok": True, "model": os.path.exists(os.getenv("YOLO_WEIGHTS", "weights/yolov8x-fire.pt"))}


def in_russia(lat, lon):
    return 41.0 <= lat <= 82.0 and (19.0 <= lon <= 180.0 or -180.0 <= lon <= -168.0)


def check_bbox(lat, lon):
    if not in_russia(lat, lon):
        raise HTTPException(422, "Координаты вне территории России")


@api.get("/geo/search", dependencies=[Depends(limiter(60))])
async def geo_search(q: str, lang: str = "ru"):
    q = q.strip()
    if len(q) < 2:
        return []

    # 1. Local — always, instant
    local = sv.local_city_search(q, limit=8)

    # 2. Nominatim — only if local is empty or has few results
    remote: list[dict] = []
    if len(local) < 5:
        try:
            remote = await sv.nominatim_search(q, limit=8, lang=lang)
        except Exception as e:
            print(f"[kedr] nominatim search failed: {e}", flush=True)

    # 3. Merge with deduplication by rounded coordinates
    seen, merged = set(), []
    for p in local + remote:
        key = (round(p["lat"], 2), round(p["lon"], 2))
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)

    return merged[:12]


@api.get("/geo/weather")
async def geo_weather(lat: float, lon: float):
    """Diagnostics: shows real Open-Meteo data or the exact error."""
    w, err = await sv.weather(lat, lon)
    return {"ok": w is not None, "data": w, "error": err}


async def geo_context(lat: float, lon: float, radius_m: int):
    """Item 4/7: weather, region, infrastructure and fuel zone for a point, all fetched in parallel."""
    check_bbox(lat, lon)
    radius_m = max(2000, min(int(radius_m), 30000))
    hazard_radius_m = max(radius_m, 50000)
    (facilities, src), (wx, _), (region, _), near = await asyncio.gather(
        sv.infrastructure_near(lat, lon, radius_m, hazard_radius_m), sv.weather(lat, lon),
        sv.reverse_geocode(lat, lon), sv.nearby(lat, lon, radius_m))

    # grouping by categories for UI
    groups = defaultdict(list)
    for f in facilities:
        groups[f["kind"]].append(f)

    counts = {k: len(v) for k, v in groups.items()}
    max_crit = max((f["criticality"] for f in facilities), default=0)

    return {
        "source": src,
        "ok": src != "none",
        "partial": src == "partial",
        "region": region,
        "weather": wx,
        "facilities": facilities,          # flat list, already sorted
        "groups": dict(groups),            # {kind: [facilities]}
        "counts": counts,
        "max_criticality": max_crit,       # 5 = nuclear/hydro/refinery present
        "radius_m": radius_m,
        "hazard_radius_m": hazard_radius_m,
        "fuel": sv.classify_fuel(lat, lon, near),
    }


@api.get("/geo/infrastructure")
async def geo_infrastructure(lat: float, lon: float, radius_m: int = 12000):
    return await geo_context(lat, lon, radius_m)


@api.post("/incidents/{iid}/refresh")
async def refresh(iid: int, db: Session = Depends(get_db), user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    i = db.get(Incident, iid)
    if not i or i.lat is None:
        raise HTTPException(404, "Incident with GPS not found")
    await enrich(db, i)
    db.commit()
    return out(i)


app.include_router(api)
