"""КЕДР API: JWT auth, detection, escalation, weather/hazard/evacuation, WebSockets, PDF."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from shapely.geometry import Point, mapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config as cfg
from . import services as sv
from .logging_conf import setup_logging
from .models import (
    GpsSource,
    Incident,
    Role,
    SessionLocal,
    Settlement,
    Source,
    Status,
    User,
    classify_detections,
    db_healthy,
    get_db,
    init_db,
    utcnow,
)
from .security import (
    PasswordPolicyError,
    create_access_token,
    hash_password,
    user_id_from_token,
    validate_password,
    verify_password,
)
from .seed import seed

log = logging.getLogger("kedr.api")

SECRET = cfg.SECRET
FPS = float(os.getenv("FRAME_SAMPLE_FPS") or 1)
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS") or 60)
UPLOADS = cfg.UPLOADS
for sub in ("images", "false_positives"):
    (UPLOADS / sub).mkdir(parents=True, exist_ok=True)

PROD = cfg.is_production()
SERVE_UPLOADS_PUBLIC = (os.getenv("SERVE_UPLOADS_PUBLIC") or "false").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def ensure_weights() -> None:
    """Optionally fetch detection weights on first boot when YOLO_WEIGHTS_URL is set."""
    path, url = cfg.weights_path(), (os.getenv("YOLO_WEIGHTS_URL") or "").strip()
    if not url or os.path.exists(path):
        return
    expected = cfg.weights_sha256()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = path + ".part"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=300) as r, open(tmp, "wb") as f:
            r.raise_for_status()
            total = 0
            limit = int(os.getenv("YOLO_WEIGHTS_MAX_MB") or 2048) << 20
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise RuntimeError(f"weights file exceeds YOLO_WEIGHTS_MAX_MB={limit >> 20}")
                f.write(chunk)
        if expected:
            import hashlib

            actual = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError("weights checksum mismatch (YOLO_WEIGHTS_SHA256); refusing to install")
        os.replace(tmp, path)
        log.info("weights downloaded to %s", path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


async def _firms_poll() -> int:
    """Pull NASA FIRMS hotspots when a map key is configured. 0 disables it."""
    key = (os.getenv("FIRMS_MAP_KEY") or "").strip()
    if not key:
        return 0
    from scripts.import_firms import fetch_csv, import_rows

    rows = await run_in_threadpool(fetch_csv, key,
                                   os.getenv("FIRMS_SOURCE") or "VIIRS_SNPP_NRT",
                                   os.getenv("FIRMS_BBOX") or "19,41,180,82",
                                   int(os.getenv("FIRMS_DAYS") or 1))
    created, _skipped = await run_in_threadpool(
        import_rows, rows, float(os.getenv("FIRMS_MIN_CONFIDENCE") or 0),
        float(os.getenv("FIRMS_DEDUPE_KM") or 1.0))
    return created


async def _maintenance_loop() -> None:
    """Periodic retention cleanup, draft purge and satellite polling."""
    firms_minutes = int(os.getenv("FIRMS_POLL_MINUTES") or 0)
    firms_last: dict = {}
    while True:
        try:
            await asyncio.sleep(int(os.getenv("MAINTENANCE_INTERVAL_SEC") or 3600))
            await run_in_threadpool(_prune_rate_limits)
            drafts = await run_in_threadpool(_purge_drafts)
            if drafts:
                log.info("drafts: removed %d stale upload(s)", drafts)
            if firms_minutes > 0 and time.time() - firms_last.get("at", 0) >= firms_minutes * 60:
                firms_last["at"] = time.time()
                try:
                    n = await _firms_poll()
                    if n:
                        log.info("FIRMS: imported %d satellite hotspot(s)", n)
                except Exception as exc:
                    log.warning("FIRMS poll failed: %s", exc)
            days = int(os.getenv("RETENTION_CLEAR_DAYS") or 14)
            if days > 0:
                removed = await run_in_threadpool(_purge_old_clear, days)
                if removed:
                    log.info("retention: removed %d stale CLEAR incident(s)", removed)
            inc_days = int(os.getenv("RETENTION_INCIDENT_DAYS") or 0)
            if inc_days > 0:
                removed = await run_in_threadpool(_purge_old_incidents, inc_days)
                if removed:
                    log.info("retention: removed %d old incident(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let maintenance kill the loop
            log.warning("maintenance tick failed: %s", exc)


@asynccontextmanager
async def lifespan(_):
    setup_logging()
    if PROD and cfg.seed_enabled():
        log.warning("SEED_ON_STARTUP is enabled in production — demo accounts may exist. "
                    "Remove them with `python -m scripts.manage delete-demo-users`.")
    init_db()
    if cfg.seed_enabled():
        await run_in_threadpool(seed)
    try:
        await run_in_threadpool(ensure_weights)
    except Exception as exc:
        log.warning("weights download failed: %s", exc)
    task = asyncio.create_task(_maintenance_loop())
    log.info("КЕДР API ready (env=%s, db=%s, model=%s)", cfg.env_name(), "sqlite" if cfg.IS_SQLITE else "postgres",
             "found" if sv.model_ready() else "missing")
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await sv.aclose_http()


app = FastAPI(title="КЕДР API", version="2.0.0", lifespan=lifespan,
              docs_url=None if PROD else "/docs", redoc_url=None, openapi_url=None if PROD else "/openapi.json")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

if SERVE_UPLOADS_PUBLIC:
    app.mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads")
    log.warning("SERVE_UPLOADS_PUBLIC is on: uploaded media is world-readable")

api = APIRouter(prefix="/api/v1")
oauth = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------
_hits: dict[tuple, deque] = defaultdict(deque)
_TRUSTED_NETS = [ipaddress.ip_network(n) for n in
                 (os.getenv("TRUSTED_PROXY_CIDRS") or
                  "127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7").split(",") if n.strip()]


def _is_trusted(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_NETS)


def client_ip(request: Request) -> str:
    """Real client IP, ignoring X-Forwarded-For entries injected by the client."""
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-forwarded-for") or ""
    chain = [p.strip() for p in fwd.split(",") if p.strip()]
    if not chain or not _is_trusted(peer):
        # Direct connection (or an untrusted peer): the header cannot be believed.
        return peer or "?"
    for candidate in reversed(chain):
        if not _is_trusted(candidate):
            return candidate
    return chain[0]


def _prune_rate_limits() -> None:
    now = time.monotonic()
    for key in list(_hits):
        q = _hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if not q:
            _hits.pop(key, None)


def limiter(per_minute: int, name: str = ""):
    """Tiny in-memory per-IP rate limit (single process); use Redis when scaling out."""
    def dep(request: Request):
        key = (name or request.url.path, client_ip(request))
        q, now = _hits[key], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            raise HTTPException(429, "Слишком много запросов, попробуйте через минуту")
        q.append(now)

    return dep


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
def current_user(token: str | None = Depends(oauth), db: Session = Depends(get_db)) -> User | None:
    if not token:
        return None
    uid, ver = user_id_from_token(token)
    if uid is None:
        raise HTTPException(401, "Invalid token")
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(401, "Invalid token")
    if not u.is_active:
        raise HTTPException(403, "Учётная запись отключена")
    if ver != u.token_version:
        raise HTTPException(401, "Сессия устарела, войдите заново")
    return u


def require(*roles):
    def dep(u: User | None = Depends(current_user)):
        if not u or u.role not in roles:
            raise HTTPException(403, "Forbidden")
        return u

    return dep


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class Register(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    password_confirm: str = Field(default="", max_length=256)
    full_name: str = Field(default="", max_length=255)

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Некорректный адрес почты")
        return v


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
    new_password_confirm: str = Field(default="", max_length=256)


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role.value,
            "is_demo": bool(u.is_demo), "created_at": u.created_at}


def _issue(u: User) -> dict:
    return {"access_token": create_access_token(u.id, u.token_version), "token_type": "bearer", "user": user_out(u)}


@api.post("/auth/login", dependencies=[Depends(limiter(cfg.rate_limit("RATE_LIMIT_LOGIN", 10)))])
def login(body: Login, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not verify_password(body.password, u.hashed_password):
        raise HTTPException(401, "Неверная почта или пароль")
    if not u.is_active:
        raise HTTPException(403, "Учётная запись отключена")
    return _issue(u)


@api.post("/auth/register", dependencies=[Depends(limiter(cfg.rate_limit("RATE_LIMIT_REGISTER", 8)))])
def register(body: Register, db: Session = Depends(get_db)):
    """Self-service registration: email + password (entered twice). No email codes.

    Roles are never accepted from the client — staff accounts are assigned
    server-side only.
    """
    if body.password_confirm and body.password != body.password_confirm:
        raise HTTPException(422, "Пароли не совпадают")
    try:
        validate_password(body.password)
    except PasswordPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "Пользователь с такой почтой уже есть")
    u = User(email=email, full_name=body.full_name.strip()[:255], role=Role.CITIZEN,
             hashed_password=hash_password(body.password))
    db.add(u)
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent signup with the same address
        db.rollback()
        raise HTTPException(409, "Пользователь с такой почтой уже есть") from exc
    db.refresh(u)
    log.info("new user registered: id=%s", u.id)
    return _issue(u)


@api.get("/auth/me")
def me(u: User = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    return user_out(u)


@api.post("/auth/change-password", dependencies=[Depends(limiter(10, "change-pw"))])
def change_password(body: PasswordChange, db: Session = Depends(get_db),
                    u: User = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    if not verify_password(body.current_password, u.hashed_password):
        raise HTTPException(403, "Текущий пароль неверен")
    if body.new_password_confirm and body.new_password != body.new_password_confirm:
        raise HTTPException(422, "Пароли не совпадают")
    try:
        validate_password(body.new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(422, str(exc)) from exc
    u.hashed_password = hash_password(body.new_password)
    u.token_version += 1  # invalidate every previously issued token
    db.commit()
    return _issue(u)


@api.post("/auth/logout")
def logout(db: Session = Depends(get_db), u: User = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    u.token_version += 1
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# websocket hub
# --------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.clients: list[tuple[WebSocket, Role]] = []

    async def send(self, msg, roles):
        payload = jsonable_encoder(msg)
        for ws, role in list(self.clients):
            if role not in roles:
                continue
            try:
                await asyncio.wait_for(ws.send_json(payload), timeout=5)
            except Exception:
                try:
                    self.clients.remove((ws, role))
                except ValueError:
                    pass


hub = Hub()


@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket, token: str = Query(default="")):
    """Staff alert stream.

    Preferred handshake (keeps the token out of URLs and proxy logs):
      1. client connects with subprotocol "kedr.v1";
      2. client sends {"type": "auth", "token": "<jwt>"} as the first frame.
    A `?token=` query parameter is still accepted for scripted clients, but it is
    deprecated because URLs are logged.
    """
    proto_offered = "kedr.v1" in [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",")]
    await ws.accept(subprotocol="kedr.v1" if proto_offered else None)

    supplied = token
    if not supplied:
        try:
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if isinstance(first, dict):
                supplied = str(first.get("token") or "")
        except Exception:
            await ws.close(code=4401)
            return

    uid, ver = user_id_from_token(supplied)
    role = None
    if uid is not None:
        with SessionLocal() as db:
            u = db.get(User, uid)
            if u and u.is_active and ver == u.token_version:
                role = u.role
    if role not in (Role.FORESTER, Role.MCHS):
        await ws.close(code=4401)
        return

    entry = (ws, role)
    hub.clients.append(entry)
    try:
        while True:
            # Any client traffic (including keep-alive pings) keeps the socket alive.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if entry in hub.clients:
            hub.clients.remove(entry)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_COMPACT_DROP = ("cells", "barriers")


def out(i: Incident, compact: bool = False) -> dict:
    data = {c.name: getattr(i, c.name) for c in i.__table__.columns}
    if compact and isinstance(data.get("hazard_geojson"), dict):
        data["hazard_geojson"] = _strip_geometry(data["hazard_geojson"])
    data["image_url"] = media_url(i.image_path)
    data["annotated_url"] = media_url(i.annotated_path)
    data["thumb_url"] = media_url(i.thumb_path)
    return data


def _strip_geometry(geojson: dict) -> dict:
    props = dict(geojson.get("properties") or {})
    for key in _COMPACT_DROP:
        props.pop(key, None)
    if "cell_count" not in props:
        props["cell_count"] = len((geojson.get("properties") or {}).get("cells") or [])
    return {"type": geojson.get("type", "Feature"), "geometry": geojson.get("geometry"), "properties": props}


def media_url(rel_path: str) -> str:
    if not rel_path:
        return ""
    if SERVE_UPLOADS_PUBLIC:
        return f"/uploads/{rel_path}"
    return f"/api/v1/media/{rel_path}?token={sign_media(rel_path)}"


def sign_media(rel_path: str, ttl: int | None = None) -> str:
    from .security import sign_media_token

    return sign_media_token(rel_path, ttl if ttl is not None else int(os.getenv("MEDIA_TOKEN_TTL") or 3600))


def bbox_filter(query, model, lat: float, lon: float, radius_km: float):
    """Cheap SQL pre-filter so helpers never scan the whole settlement table."""
    dlat = radius_km / 111.0
    dlon = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-3)
    return query.filter(model.lat.between(lat - dlat, lat + dlat),
                        model.lon.between(lon - dlon, lon + dlon))


def settlements_within(db: Session, lat: float, lon: float, radius_km: float,
                       min_population: int = 0) -> list[Settlement]:
    q = db.query(Settlement)
    if min_population:
        q = q.filter(Settlement.population >= min_population)
    q = bbox_filter(q, Settlement, lat, lon, radius_km)
    return [s for s in q.all() if sv.haversine_km(lat, lon, s.lat, s.lon) <= radius_km]


def _pick_fuel(db: Session, lat: float, lon: float, near: dict) -> tuple[str, bool]:
    """classify_fuel with a local-DB fallback when Overpass returned nothing.

    Returns (fuel, estimated). `estimated` is True when OSM land cover was
    unavailable and the type came from the settlement database alone — the caller
    surfaces that to the operator instead of presenting a guess as a measurement.
    """
    fuel = sv.classify_fuel(lat, lon, near)
    if not near.get("landuse") and not near.get("places"):
        if settlements_within(db, lat, lon, 15, min_population=20000):
            fuel = "urban"
        elif settlements_within(db, lat, lon, 30):
            fuel = "mixed"
        else:
            fuel = "forest"
        return fuel, True
    return fuel, False


def in_russia(lat: float, lon: float) -> bool:
    return 41.0 <= lat <= 82.0 and (19.0 <= lon <= 180.0 or -180.0 <= lon <= -168.0)


def check_bbox(lat: float, lon: float) -> None:
    if not in_russia(lat, lon):
        raise HTTPException(422, "Координаты вне территории России")


# --------------------------------------------------------------------------
# enrichment
# --------------------------------------------------------------------------
async def _gather_with_deadline(deadline_s: float, **coros) -> dict:
    """Await several coroutines but never longer than `deadline_s`.

    Pending coroutines are cancelled and reported as `None`, so the caller can
    substitute a fallback instead of waiting on a dead external service.
    """
    tasks = {name: asyncio.create_task(coro) for name, coro in coros.items()}
    done, pending = await asyncio.wait(tasks.values(), timeout=deadline_s)
    for task in pending:
        task.cancel()
    out: dict = {}
    for name, task in tasks.items():
        if task in done:
            try:
                out[name] = task.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("enrichment step %s failed: %s", name, exc)
                out[name] = None
        else:
            log.warning("enrichment step %s exceeded the %.0fs deadline", name, deadline_s)
            out[name] = None
    return out


async def enrich(db: Session, i: Incident) -> None:
    """Weather + region + OSM context in parallel; then the spread polygon.

    Blocking work (geometry) runs in a thread pool so the event loop — and with
    it every other request and WebSocket alert — stays responsive.
    """
    hazard_radius = int(os.getenv("HAZARD_RADIUS_M") or 15000)
    # Hard deadline on the external phase: a slow or dead public service must delay
    # an incident, never block it. Whatever finished in time is used; the rest
    # degrades to the local database.
    ext = await _gather_with_deadline(
        float(os.getenv("ENRICH_DEADLINE_SEC") or 20),
        weather=sv.weather(i.lat, i.lon),
        geocode=sv.reverse_geocode(i.lat, i.lon),
        nearby=sv.nearby(i.lat, i.lon),
        infra=sv.infrastructure_near(i.lat, i.lon, 15000, hazard_radius),
    )
    wx, _wx_err = ext["weather"] if isinstance(ext["weather"], tuple) else (None, "deadline")
    region, cc = ext["geocode"] if isinstance(ext["geocode"], tuple) else ("", "")
    near = ext["nearby"] if isinstance(ext["nearby"], dict) else {"places": [], "landuse": [], "source": "none"}
    facilities, infra_src = ext["infra"] if isinstance(ext["infra"], tuple) else ([], "none")
    # Availability of OSM data, captured *before* `src` is repurposed below as the
    # settlement-selection mode. A step is degraded when OSM could not be reached —
    # not merely when it returned no objects (the helpers report "none" for both,
    # and network errors are swallowed on purpose).
    near_src = near.get("source", "none")
    osm_available = near_src not in ("none",) or infra_src != "none"
    degraded = near_src == "none" or infra_src == "none"

    if wx:
        i.wind_speed_ms = wx["wind_speed_ms"]
        i.wind_dir_deg = wx["wind_dir_deg"]
        i.temp_c = wx["temp_c"]
        i.humidity_pct = wx["humidity_pct"]
    else:
        i.wind_speed_ms = i.wind_dir_deg = i.temp_c = i.humidity_pct = None
    i.weather_fallback = 0 if wx else 1  # 1 = weather unavailable (never invented)
    i.region_name = region or ""

    if not i.region_name:
        nearest = settlements_within(db, i.lat, i.lon, 50)
        if nearest:
            s = min(nearest, key=lambda s: sv.haversine_km(i.lat, i.lon, s.lat, s.lon))
            d = sv.haversine_km(i.lat, i.lon, s.lat, s.lon)
            i.region_name = f"{s.region}, {s.name} (~{d:.0f} км)"

    if cc and cc != "ru":
        raise HTTPException(422, "Точка находится вне России")

    fuel, fuel_estimated = _pick_fuel(db, i.lat, i.lon, near)
    terrain = sv.terrain_from_near(near, fuel)
    slope = sv.slope_deg_for_terrain(terrain)

    # Physical spread model (Rothermel + Anderson ellipse); burn time is an
    # explicit scenario input, not a fuel property.
    poly, cells_data, cell_count, diag = await run_in_threadpool(
        sv.hazard_polygon, i.lat, i.lon, i.wind_speed_ms, i.wind_dir_deg, i.temp_c, i.humidity_pct,
        fuel, None, slope)
    ros = diag["ros_m_per_min"]
    burn_minutes = diag["burn_minutes"]

    barriers = await sv.check_fire_barriers(i.lat, i.lon, ros * burn_minutes, poly)
    places_osm = near.get("places") or []
    src = near.get("source", "none")

    selected, picked = sv.select_places(poly, places_osm, i.lat, i.lon, extra_km=0)
    if not selected:
        padded = poly.buffer(1.0 / 111.32)  # 1 km around the polygon
        local = [{"name": s.name, "lat": s.lat, "lon": s.lon, "type": "city", "population": s.population}
                 for s in db.query(Settlement).all() if padded.covers(Point(s.lon, s.lat))]
        selected, picked = local, ("zone" if local else "none")
        src = "local" if local else "none"

    i.evacuation = sv.evacuation_plan(poly, i.lat, i.lon, selected, facilities, i.wind_dir_deg)
    max_crit = max((f["criticality"] for f in facilities), default=0)

    i.hazard_geojson = {"type": "Feature", "geometry": mapping(poly),
                        "properties": {
                            "mode": "wind" if wx else "circle",
                            "settlements": src, "picked": picked, "fuel": fuel, "terrain": terrain,
                            "fuel_estimated": fuel_estimated, "enrichment_degraded": degraded,
                            "nearby_source": near_src, "infra_source": infra_src,
                            "osm_available": osm_available,
                            "slope_deg": slope,
                            # physical diagnostics, all SI
                            **{k: v for k, v in diag.items() if k != "cell_count"},
                            "overpass_available": osm_available,
                            "cells": cells_data, "cell_count": cell_count,
                            "barriers": barriers, "barrier_count": len(barriers),
                            "max_criticality": max_crit, "facilities_count": len(facilities),
                        }}


async def notify(i: Incident) -> None:
    if i.status == Status.PENDING_VERIFICATION:
        await hub.send({"type": "PENDING_VERIFICATION", "incident": out(i, compact=True)}, {Role.FORESTER, Role.MCHS})
    elif i.status in (Status.CRITICAL_ALERT, Status.CONFIRMED):
        await hub.send({"type": "CRITICAL_ALERT", "incident": out(i, compact=True)}, {Role.MCHS})


def fold_clear(db: Session, i: Incident) -> Incident | None:
    """CLEAR rows are near-misses: fold repeats from the same spot into the first row."""
    tol = float(os.getenv("CLEAR_DEDUPE_KM") or 0.5)
    if i.lat is None or i.lon is None:
        return None
    candidates = bbox_filter(db.query(Incident).filter(Incident.id != i.id, Incident.status == Status.CLEAR,
                                                      Incident.lat.isnot(None), Incident.lon.isnot(None)),
                             Incident, i.lat, i.lon, max(tol, 0.5)).all()
    for r in candidates:
        if sv.haversine_km(i.lat, i.lon, r.lat, r.lon) <= tol:
            r.confidence = max(r.confidence or 0.0, i.confidence or 0.0)
            merged = (r.detections or []) + (i.detections or [])
            r.detections = merged[: int(os.getenv("MAX_STORED_DETECTIONS") or 200)]
            r.created_at = utcnow()
            db.delete(i)
            return r
    return None


_SOURCE_BY_VIDEO = {True: Source.VIDEO, False: Source.PHOTO}


async def create_from_analysis(db: Session, res, source: Source, lat, lon, gps, user) -> Incident:
    conf, dets, raw, ann = res
    status = classify_detections(conf, dets)
    i = Incident(source=source, confidence=conf, status=status, detections=dets,
                 lat=lat, lon=lon, gps_source=gps, reporter_id=user.id if user else None)
    db.add(i)
    db.flush()  # assign the id; the row is committed only once the record is complete

    if i.status == Status.CLEAR and lat is not None:
        try:
            await enrich(db, i)
        except HTTPException:
            db.rollback()
            raise
        except Exception as exc:
            log.warning("enrich failed for #%s: %s", i.id, exc)
        dup = fold_clear(db, i)  # may delete `i`; its frames were never written yet
        db.commit()
        if dup is not None:
            return dup

    written: list[Path] = []
    if sv.CV2_AVAILABLE and raw is not None:
        try:
            paths = await run_in_threadpool(_save_incident_images, i, raw, ann)
            i.image_path, i.annotated_path, i.thumb_path = paths
            written = [UPLOADS / p for p in paths if p]
        except Exception as exc:
            log.warning("could not persist frames for #%s: %s", i.id, exc)

    if i.status != Status.CLEAR and lat is not None:
        try:
            await enrich(db, i)
        except HTTPException:
            # e.g. the point turned out to be outside Russia: do not keep a stray record
            db.rollback()
            for p in written:
                p.unlink(missing_ok=True)
            raise
        except Exception as exc:
            log.warning("enrich failed for #%s: %s", i.id, exc)

    db.commit()
    await notify(i)
    return i


def _save_incident_images(i: Incident, raw, ann) -> tuple[str, str, str]:
    """Persist frames as compressed JPEGs plus a thumbnail (never at full weight)."""
    stem = f"{i.id}_{uuid.uuid4().hex[:8]}"
    img_dir = UPLOADS / "images"
    raw_p, ann_p, thumb_p = (img_dir / f"{stem}.jpg", img_dir / f"{stem}_ann.jpg",
                             img_dir / f"{stem}_thumb.jpg")
    ok_raw = sv.save_image(raw_p, raw)
    ok_ann = sv.save_image(ann_p, ann)
    source = ann_p if ok_ann else raw_p
    ok_thumb = sv.make_thumbnail(source, thumb_p) if source.exists() else False
    return (f"images/{stem}.jpg" if ok_raw else "",
            f"images/{stem}_ann.jpg" if ok_ann else "",
            f"images/{stem}_thumb.jpg" if ok_thumb else "")


# --------------------------------------------------------------------------
# guided intake: analyse first, create the incident only after the operator
# confirms (or corrects) the shooting location
# --------------------------------------------------------------------------
DRAFT_TTL_SEC = int(os.getenv("DRAFT_TTL_SEC") or 1800)
_drafts: dict[str, dict] = {}


def _draft_dir(draft_id: str) -> Path:
    return UPLOADS / "drafts" / draft_id


def _purge_drafts() -> int:
    now = time.time()
    removed = 0
    for did in [k for k, v in _drafts.items() if now - v["created"] > DRAFT_TTL_SEC]:
        _drafts.pop(did, None)
        shutil.rmtree(_draft_dir(did), ignore_errors=True)
        removed += 1
    # orphaned directories (e.g. after a restart) are removed as well
    root = UPLOADS / "drafts"
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime > DRAFT_TTL_SEC:
                    shutil.rmtree(d, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed


@api.post("/incidents/analyze", dependencies=[Depends(limiter(cfg.rate_limit("RATE_LIMIT_PER_MIN", 10)))])
async def analyze_only(file: UploadFile = File(...), user: User | None = Depends(current_user)):
    """Run detection and return the annotated frame WITHOUT creating an incident.

    The client then shows the result, asks the operator to confirm the location
    (EXIF may be missing or wrong) and calls /incidents/confirm.
    """
    ctype = (file.content_type or "").lower()
    if not (ctype.startswith("image/") or _is_video_upload(ctype)):
        raise HTTPException(415, "Допустимы только фото и видео")
    is_video = _is_video_upload(ctype)
    if is_video and user is None:
        raise HTTPException(401, "Загрузка видео доступна только авторизованным пользователям")

    tmp_name = await stream_upload(file)
    exif = None
    try:
        if not is_video:
            exif = sv.exif_gps(tmp_name)
        try:
            res = await run_in_threadpool(sv.analyze, tmp_name, is_video, FPS,
                                          int(MAX_VIDEO_SECONDS * FPS) if is_video else None)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    if not res:
        raise HTTPException(422, "Не удалось прочитать загруженный файл")

    conf, dets, raw, ann = res
    draft_id = uuid.uuid4().hex
    d = _draft_dir(draft_id)
    d.mkdir(parents=True, exist_ok=True)
    ok_ann = await run_in_threadpool(sv.save_image, d / "annotated.jpg", ann)
    await run_in_threadpool(sv.save_image, d / "raw.jpg", raw)
    if not ok_ann:
        await run_in_threadpool(sv.save_image, d / "annotated.jpg", raw)
    await run_in_threadpool(sv.make_thumbnail, d / "annotated.jpg", d / "thumb.jpg")

    lat = lon = None
    gps = GpsSource.NONE
    if exif:
        lat, lon, gps = exif[0], exif[1], GpsSource.EXIF
    if lat is not None and not in_russia(lat, lon):
        lat = lon = None
        gps = GpsSource.NONE

    status = classify_detections(conf, dets)
    _drafts[draft_id] = {"created": time.time(), "confidence": conf, "detections": dets,
                         "status": status, "source": Source.VIDEO if is_video else Source.PHOTO,
                         "lat": lat, "lon": lon, "gps_source": gps,
                         "reporter_id": user.id if user else None}
    log.info("draft %s: %d detection(s), status=%s, exif=%s", draft_id[:8], len(dets), status.value, bool(exif))
    return {
        "draft_id": draft_id,
        "confidence": conf,
        "detections": dets,
        "status": status.value,
        "has_exif": bool(exif),
        "lat": lat, "lon": lon, "gps_source": gps.value,
        "annotated_url": media_url(f"drafts/{draft_id}/annotated.jpg"),
        "raw_url": media_url(f"drafts/{draft_id}/raw.jpg"),
        "thumb_url": media_url(f"drafts/{draft_id}/thumb.jpg"),
        "expires_in": DRAFT_TTL_SEC,
    }


class Confirm(BaseModel):
    draft_id: str = Field(min_length=8, max_length=64)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=200)


@api.post("/incidents/confirm", dependencies=[Depends(limiter(30, "confirm"))])
async def confirm_incident(body: Confirm, db: Session = Depends(get_db),
                           user: User | None = Depends(current_user)):
    """Create the incident from a draft, with the location the operator confirmed."""
    draft = _drafts.pop(body.draft_id, None)
    d = _draft_dir(body.draft_id)
    if draft is None:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(410, "Черновик устарел, загрузите файл заново")

    if body.lat is not None and body.lon is not None:
        check_bbox(body.lat, body.lon)
        lat, lon, gps = body.lat, body.lon, GpsSource.MANUAL
    else:
        lat, lon, gps = draft["lat"], draft["lon"], draft["gps_source"]

    i = Incident(source=draft["source"], confidence=draft["confidence"], status=draft["status"],
                 detections=draft["detections"], lat=lat, lon=lon, gps_source=gps,
                 reporter_id=user.id if user else draft["reporter_id"])
    db.add(i)
    db.flush()

    # move the draft frames into the permanent (compressed) store
    if d.is_dir():
        stem = f"{i.id}_{uuid.uuid4().hex[:8]}"
        img_dir = UPLOADS / "images"
        try:
            if (d / "raw.jpg").exists():
                shutil.move(str(d / "raw.jpg"), str(img_dir / f"{stem}.jpg"))
                i.image_path = f"images/{stem}.jpg"
            if (d / "annotated.jpg").exists():
                shutil.move(str(d / "annotated.jpg"), str(img_dir / f"{stem}_ann.jpg"))
                i.annotated_path = f"images/{stem}_ann.jpg"
            if (d / "thumb.jpg").exists():
                shutil.move(str(d / "thumb.jpg"), str(img_dir / f"{stem}_thumb.jpg"))
                i.thumb_path = f"images/{stem}_thumb.jpg"
        except OSError as exc:
            log.warning("could not move draft frames for #%s: %s", i.id, exc)
        shutil.rmtree(d, ignore_errors=True)
    db.commit()

    if i.status != Status.CLEAR and lat is not None:
        try:
            await enrich(db, i)      # rewrites region_name, so the note goes after it
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            log.warning("enrich failed for #%s: %s", i.id, exc)
    _append_note(i, body.note)
    db.commit()
    await notify(i)
    return out(i)


async def stream_upload(file: UploadFile) -> str:
    """Stream an upload to a temp file, enforcing MAX_UPLOAD_MB. Returns the temp path."""
    limit = cfg.MAX_UPLOAD_MB << 20
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "f").suffix, delete=False) as tmp:
        size = 0
        try:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Файл больше {cfg.MAX_UPLOAD_MB} МБ")
                tmp.write(chunk)
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        return tmp.name


_VIDEO_TYPES = ("video/mp4", "video/avi", "video/x-msvideo", "video/quicktime", "video/x-matroska",
                "video/webm", "video/x-ms-wmv", "video/mpeg", "video/ogg", "video/3gpp")


def _is_video_upload(content_type: str) -> bool:
    ctype = (content_type or "").lower()
    return ctype.startswith("video/") or any(ctype.startswith(t) for t in _VIDEO_TYPES)


def _assert_fresh_incident(i: Incident | None, iid: int) -> Incident:
    if not i:
        raise HTTPException(404, "Инцидент не найден")
    return i


# --------------------------------------------------------------------------
# incident intake
# --------------------------------------------------------------------------
@api.post("/incidents/report", dependencies=[Depends(limiter(cfg.rate_limit("RATE_LIMIT_PER_MIN", 10)))])
async def report(file: UploadFile = File(...), lat: float | None = Form(None), lon: float | None = Form(None),
                 location_note: str | None = Form(None),
                 db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ctype = (file.content_type or "").lower()
    if not (ctype.startswith("image/") or _is_video_upload(ctype)):
        raise HTTPException(415, "Допустимы только фото и видео (MP4, AVI, MOV, MKV, WMV, FLV, WebM)")
    is_video = _is_video_upload(ctype)
    if is_video and user is None:
        raise HTTPException(401, "Загрузка видео доступна только авторизованным пользователям")

    tmp_name = await stream_upload(file)
    gps = GpsSource.DEVICE if lat is not None and lon is not None else GpsSource.NONE
    try:
        if not is_video and (exif := sv.exif_gps(tmp_name)):
            (lat, lon), gps = exif, GpsSource.EXIF
        if lat is not None and lon is not None and not in_russia(lat, lon):
            lat = lon = None
            gps = GpsSource.NONE
        max_frames = int(MAX_VIDEO_SECONDS * FPS) if is_video else None
        try:
            res = await run_in_threadpool(sv.analyze, tmp_name, is_video, FPS, max_frames)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    if not res:
        raise HTTPException(422, "Не удалось прочитать загруженный файл (повреждён или неподдерживаемый формат)")
    i = await create_from_analysis(db, res, _SOURCE_BY_VIDEO[is_video], lat, lon, gps, user)
    if location_note and location_note.strip():
        i.region_name = (i.region_name + " · " if i.region_name else "") + location_note.strip()[:200]
        db.commit()
    return out(i)


class Rtsp(BaseModel):
    url: str = Field(min_length=5, max_length=2048)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    seconds: int = Field(10, ge=1, le=120)


_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10")]


def _assert_safe_stream_url(url: str) -> None:
    """Block SSRF: only rtsp(s)/http(s), no credentials, no private/reserved targets."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("rtsp", "rtsps", "http", "https"):
        raise HTTPException(422, "Недопустимая схема URL (разрешены rtsp, rtsps, http, https)")
    if parsed.username or parsed.password:
        raise HTTPException(422, "Учётные данные в URL запрещены")
    host = parsed.hostname
    if not host:
        raise HTTPException(422, "Некорректный URL")
    try:
        import socket

        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise HTTPException(422, f"Не удалось разрешить хост: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _BLOCKED_NETS):
            raise HTTPException(422, "Запрещено обращаться к внутренним и служебным адресам")


@api.post("/incidents/rtsp", dependencies=[Depends(limiter(6, "rtsp"))])
async def scan_rtsp(body: Rtsp, db: Session = Depends(get_db),
                    user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    check_bbox(body.lat, body.lon)
    _assert_safe_stream_url(body.url)
    try:
        res = await run_in_threadpool(sv.analyze, body.url, True, FPS, body.seconds)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if not res:
        raise HTTPException(422, "Кадры из потока не получены")
    return out(await create_from_analysis(db, res, Source.RTSP, body.lat, body.lon, GpsSource.MANUAL, user))


class Manual(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    note: str | None = Field(default=None, max_length=200)


@api.post("/incidents/manual", dependencies=[Depends(limiter(20, "manual"))])
async def manual(body: Manual, db: Session = Depends(get_db), user: User = Depends(require(Role.MCHS))):
    """MCHS manual entry without a photo = dispatcher confirmation."""
    check_bbox(body.lat, body.lon)
    i = Incident(source=Source.MANUAL, status=Status.CONFIRMED, confidence=1.0, lat=body.lat, lon=body.lon,
                 gps_source=GpsSource.MANUAL, reporter_id=user.id, detections=[])
    db.add(i)
    db.flush()
    try:
        await enrich(db, i)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        log.warning("enrich failed for #%s: %s", i.id, exc)
    _append_note(i, body.note)
    db.commit()
    await notify(i)
    return out(i)


def _append_note(i: Incident, note: str | None) -> None:
    """Attach an operator note without losing the geocoded region name."""
    if not note or not note.strip():
        return
    base = (i.region_name or "").split(" · ")[0]
    i.region_name = (base + " · " if base else "") + note.strip()[:200]


@api.post("/incidents/manual-photo", dependencies=[Depends(limiter(20, "manual-photo"))])
async def manual_photo(
    file: UploadFile | None = File(None),
    lat: float = Form(...),
    lon: float = Form(...),
    verdict: str = Form("auto"),
    db: Session = Depends(get_db),
    user: User = Depends(require(Role.FORESTER, Role.MCHS)),
):
    """Forester: photo required, but a human may confirm what the model missed.

    MCHS: photo optional; `verdict=critical` raises a critical alert, otherwise the
    dispatcher confirmation wins even when the network is unsure.
    """
    check_bbox(lat, lon)
    if verdict not in ("auto", "confirm", "critical"):
        raise HTTPException(422, "verdict должен быть auto, confirm или critical")
    if user.role == Role.FORESTER and file is None:
        raise HTTPException(422, "Лесничему нужно приложить фото очага")

    conf, dets, raw, ann = None, [], None, None
    if file is not None:
        ctype = (file.content_type or "").lower()
        if not ctype.startswith("image/"):
            raise HTTPException(415, "Нужно фото для подтверждения")
        tmp_name = await stream_upload(file)
        try:
            try:
                res = await run_in_threadpool(sv.analyze, tmp_name, False, FPS)
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if not res:
            raise HTTPException(422, "Не удалось прочитать фото")
        conf, dets, raw, ann = res
        if user.role == Role.FORESTER and verdict == "auto" and classify_detections(conf, dets) == Status.CLEAR:
            raise HTTPException(422, "На фото не обнаружен огонь/дым. Отправьте снимок очага "
                                     "или подтвердите вручную (verdict=confirm).")

    if user.role == Role.MCHS:
        status = Status.CRITICAL_ALERT if verdict == "critical" else Status.CONFIRMED
        confidence = max(conf or 0.0, 0.5)
    elif verdict == "confirm":
        status, confidence = Status.CONFIRMED, max(conf or 0.0, 0.5)  # explicit human override
    else:
        status, confidence = classify_detections(conf or 0.0, dets), (conf or 0.0)

    i = Incident(source=Source.MANUAL, confidence=confidence, status=status, lat=lat, lon=lon,
                 gps_source=GpsSource.MANUAL, reporter_id=user.id, detections=dets or [])
    db.add(i)
    db.flush()
    written: list[Path] = []
    if raw is not None and sv.CV2_AVAILABLE:
        try:
            paths = await run_in_threadpool(_save_incident_images, i, raw, ann)
            i.image_path, i.annotated_path, i.thumb_path = paths
            written = [UPLOADS / p for p in paths if p]
        except Exception as exc:
            log.warning("could not persist frames for #%s: %s", i.id, exc)
    try:
        await enrich(db, i)
    except HTTPException:
        db.rollback()
        for p in written:
            p.unlink(missing_ok=True)
        raise
    except Exception as exc:
        log.warning("enrich failed for #%s: %s", i.id, exc)
    db.commit()
    await notify(i)
    return out(i)


# --------------------------------------------------------------------------
# incident reads
# --------------------------------------------------------------------------
@api.get("/incidents/public")
def public_incidents(db: Session = Depends(get_db)):
    rows = (db.query(Incident).filter(Incident.status.in_([Status.CONFIRMED, Status.CRITICAL_ALERT]),
                                      Incident.lat.isnot(None)).order_by(Incident.created_at.desc()).limit(200))
    return [{k: getattr(r, k) for k in ("id", "lat", "lon", "status", "confidence", "created_at", "region_name")}
            for r in rows]


@api.get("/incidents/general-report")
def general_report(db: Session = Depends(get_db), user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """Aggregated summary over recent incidents (no large geometry payloads)."""
    incidents = db.query(Incident).order_by(Incident.created_at.desc()).limit(500).all()
    report = {"total": len(incidents), "by_status": {}, "by_fuel": {}, "by_source": {}, "recent": []}
    for i in incidents:
        report["by_status"][i.status.value] = report["by_status"].get(i.status.value, 0) + 1
        report["by_source"][i.source.value] = report["by_source"].get(i.source.value, 0) + 1
        if isinstance(i.hazard_geojson, dict):
            fuel = (i.hazard_geojson.get("properties") or {}).get("fuel", "unknown")
            report["by_fuel"][fuel] = report["by_fuel"].get(fuel, 0) + 1
        report["recent"].append({
            "id": i.id, "status": i.status.value, "source": i.source.value, "confidence": i.confidence,
            "lat": i.lat, "lon": i.lon, "region": i.region_name,
            "created_at": (i.created_at or utcnow()).isoformat(),
            "evacuation_count": len(i.evacuation or []),
        })
    return report


@api.get("/incidents")
def list_incidents(status: str | None = None, include_clear: int = 0,
                   limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db), user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """Staff feed. Compact payload (no per-cell geometry); use /incidents/{id} for details."""
    q = db.query(Incident)
    if status:
        try:
            q = q.filter(Incident.status == Status(status))
        except ValueError as exc:
            raise HTTPException(422, f"Неизвестный статус: {status}") from exc
    elif not include_clear:
        q = q.filter(Incident.status != Status.CLEAR)
    rows = q.order_by(Incident.created_at.desc()).offset(offset).limit(limit).all()
    return [out(i, compact=True) for i in rows]


@api.get("/incidents/{iid}")
def get_incident(iid: int, db: Session = Depends(get_db),
                 user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    return out(_assert_fresh_incident(db.get(Incident, iid), iid))


class LocationPatch(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    note: str | None = Field(default=None, max_length=200)


@api.patch("/incidents/{iid}/location", dependencies=[Depends(limiter(30, "locate"))])
async def patch_location(iid: int, body: LocationPatch, db: Session = Depends(get_db),
                         user: User = Depends(require(Role.CITIZEN, Role.FORESTER, Role.MCHS))):
    """Refine the point by hand — provenance is recorded as MANUAL and the zone recomputed."""
    i = _assert_fresh_incident(db.get(Incident, iid), iid)
    if user.role == Role.CITIZEN and i.reporter_id != user.id:
        raise HTTPException(403, "Forbidden")
    check_bbox(body.lat, body.lon)
    i.lat, i.lon, i.gps_source = body.lat, body.lon, GpsSource.MANUAL
    if i.status != Status.CLEAR:
        try:
            await enrich(db, i)
        except HTTPException as exc:
            db.rollback()
            raise exc
        except Exception as exc:
            log.warning("enrich failed: %s", exc)
    _append_note(i, body.note)  # after enrich, which rewrites region_name
    db.commit()
    return out(i)


@api.delete("/incidents/{iid}")
async def delete_incident(iid: int, db: Session = Depends(get_db),
                          user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    i = _assert_fresh_incident(db.get(Incident, iid), iid)
    _delete_incident_files(i)
    db.delete(i)
    db.commit()
    await hub.send({"type": "INCIDENT_DELETED", "id": iid}, {Role.FORESTER, Role.MCHS})
    return {"ok": True}


def _delete_incident_files(i: Incident) -> None:
    for rel in (i.image_path, i.annotated_path):
        p = _safe_upload_path(rel)
        if p and p.exists() and p.is_file():
            try:
                p.unlink()
            except OSError as exc:
                log.warning("could not delete %s: %s", p, exc)


def _safe_upload_path(rel: str | None) -> Path | None:
    """Resolve a stored relative path and refuse anything outside the uploads dir."""
    if not rel:
        return None
    base = UPLOADS.resolve()
    candidate = (base / rel).resolve()
    if candidate != base and base not in candidate.parents:
        log.warning("refusing to touch a path outside uploads: %s", rel)
        return None
    return candidate


class Review(BaseModel):
    verdict: str


@api.post("/incidents/{iid}/review")
async def review(iid: int, body: Review, db: Session = Depends(get_db),
                 user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    """Human decision on a PENDING_VERIFICATION or CRITICAL_ALERT case."""
    i = _assert_fresh_incident(db.get(Incident, iid), iid)
    if body.verdict not in ("confirm", "false_alarm"):
        raise HTTPException(422, "verdict должен быть confirm или false_alarm")
    if i.status not in (Status.PENDING_VERIFICATION, Status.CRITICAL_ALERT):
        raise HTTPException(409, "Инцидент не находится на рассмотрении")

    if body.verdict == "false_alarm":
        i.status = Status.FALSE_ALARM
        src = _safe_upload_path(i.image_path)
        if src and src.exists() and src.is_file():  # clean image becomes a hard negative
            dest = UPLOADS / "false_positives" / src.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            i.image_path = f"false_positives/{src.name}"
        db.commit()
    else:
        i.status = Status.CONFIRMED
        i.confidence = max(i.confidence or 0.0, 0.5)  # human confirmation is evidence
        db.commit()  # persist the status before the slow enrich
        if i.hazard_geojson is None and i.lat is not None or isinstance(i.hazard_geojson, dict) and "cells" not in (i.hazard_geojson.get("properties") or {}):
            try:
                await enrich(db, i)
            except Exception as exc:
                log.warning("enrich failed: %s", exc)
            db.commit()

    i.reviewed_by_id, i.reviewed_at = user.id, utcnow()
    db.commit()
    if i.status == Status.CONFIRMED:
        await hub.send({"type": "CRITICAL_ALERT", "incident": out(i, compact=True)}, {Role.MCHS})
    return out(i)


@api.get("/incidents/{iid}/report.pdf")
def report_pdf(iid: int, db: Session = Depends(get_db), user: User = Depends(require(Role.MCHS))):
    i = _assert_fresh_incident(db.get(Incident, iid), iid)
    if i.lat is None:
        raise HTTPException(404, "У инцидента нет координат")
    try:
        pdf = sv.make_pdf(i, UPLOADS)
    except Exception as exc:
        log.exception("pdf generation failed for #%s", iid)
        raise HTTPException(500, f"Не удалось сформировать PDF: {exc}") from exc
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="kedr-{iid}.pdf"'})


@api.post("/incidents/{iid}/refresh", dependencies=[Depends(limiter(20, "refresh"))])
async def refresh(iid: int, db: Session = Depends(get_db),
                  user: User = Depends(require(Role.FORESTER, Role.MCHS))):
    i = _assert_fresh_incident(db.get(Incident, iid), iid)
    if i.lat is None:
        raise HTTPException(404, "У инцидента нет координат")
    await enrich(db, i)
    db.commit()
    return out(i)


# --------------------------------------------------------------------------
# media (signed URLs)
# --------------------------------------------------------------------------
@api.get("/media/{path:path}")
def get_media(path: str, token: str = ""):
    from .security import verify_media_token

    if not verify_media_token(path, token):
        raise HTTPException(403, "Ссылка недействительна или истекла")
    p = _safe_upload_path(path)
    if p is None or not p.exists() or not p.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=300",
                                    "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------------------------
# health + geo
# --------------------------------------------------------------------------
@api.get("/health")
def health():
    status = sv.model_status()
    return {"ok": True, "db": db_healthy(), "model": status["weights_found"] and status["available"],
            "model_info": status}


@api.get("/geo/search", dependencies=[Depends(limiter(60, "geo-search"))])
async def geo_search(q: str, lang: str = "ru"):
    q = q.strip()
    if len(q) < 2:
        return []
    local = sv.local_city_search(q, limit=8)
    remote: list[dict] = []
    if len(local) < 5:
        try:
            remote = await sv.yandex_suggest(q, limit=8)
        except Exception as exc:
            log.debug("yandex suggest failed: %s", exc)
    if len(local) + len(remote) < 5:
        try:
            remote += await sv.nominatim_search(q, limit=8, lang=lang)
        except Exception as exc:
            log.warning("nominatim search failed: %s", exc)
    seen, merged = set(), []
    for p in local + remote:
        key = (round(p["lat"], 2), round(p["lon"], 2))
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    return merged[:12]


@api.get("/geo/weather", dependencies=[Depends(limiter(30, "geo-weather"))])
async def geo_weather(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    w, err = await sv.weather(lat, lon)
    return {"ok": w is not None, "data": w, "error": err}


async def geo_context(db: Session, lat: float, lon: float, radius_m: int):
    """Weather, region, infrastructure and fuel zone for a point, fetched in parallel."""
    check_bbox(lat, lon)
    radius_m = max(2000, min(int(radius_m), 30000))
    hazard_radius_m = max(radius_m, int(os.getenv("HAZARD_RADIUS_M") or 15000))
    (facilities, src), (wx, _), (region, _), near = await asyncio.gather(
        sv.infrastructure_near(lat, lon, radius_m, hazard_radius_m), sv.weather(lat, lon),
        sv.reverse_geocode(lat, lon), sv.nearby(lat, lon, radius_m))

    groups = defaultdict(list)
    for f in facilities:
        groups[f["kind"]].append(f)
    return {
        "source": src, "ok": src != "none", "partial": src == "partial", "region": region, "weather": wx,
        "facilities": facilities, "groups": dict(groups), "counts": {k: len(v) for k, v in groups.items()},
        "max_criticality": max((f["criticality"] for f in facilities), default=0),
        "radius_m": radius_m, "hazard_radius_m": hazard_radius_m,
        "fuel": _pick_fuel(db, lat, lon, near)[0],
    }


@api.get("/geo/infrastructure", dependencies=[Depends(limiter(20, "geo-infra"))])
async def geo_infrastructure(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                             radius_m: int = 12000, db: Session = Depends(get_db)):
    return await geo_context(db, lat, lon, radius_m)


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------
def _purge_old_clear(days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.query(Incident).filter(Incident.status == Status.CLEAR, Incident.created_at < cutoff).all()
        for r in rows:
            _delete_incident_files(r)
            db.delete(r)
        db.commit()
        return len(rows)


def _purge_old_incidents(days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    with SessionLocal() as db:
        rows = db.query(Incident).filter(Incident.created_at < cutoff).all()
        for r in rows:
            _delete_incident_files(r)
            db.delete(r)
        db.commit()
        return len(rows)


app.include_router(api)
