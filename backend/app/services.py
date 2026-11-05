"""Detection, EXIF, weather, geocoding, hazard spread (Rothermel + cellular automata),
evacuation planning and PDF reports.

Design rules kept throughout this module:
  * heavy/optional dependencies (torch, ultralytics, cv2, osmnx) are imported
    lazily so the API and the management CLI boot without them;
  * every external HTTP call is bounded by a timeout, cached, and never issued
    synchronously from the event loop;
  * nothing here invents weather or location data.
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import io
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import httpx
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union

from . import fire_model as fm
from .config import OVERPASS_TIMEOUT, detect_device, weights_path, weights_sha256

# zone geometry (area / perimeter / diameter) lives with the fire model
zone_metrics = fm.zone_metrics

log = logging.getLogger("kedr.services")

# --------------------------------------------------------------------------
# optional dependencies
# --------------------------------------------------------------------------
try:
    import cv2
    CV2_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    cv2 = None
    CV2_AVAILABLE = False
    log.warning("OpenCV is not installed: photo/video analysis is disabled")

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    Image = None
    PIL_AVAILABLE = False

MODEL_IMPORT_ERROR: str | None = None

_model = None
_model_path: str | None = None
_model_lock = threading.Lock()


def vision_available() -> bool:
    """True when a detection backend can actually be constructed."""
    try:
        import ultralytics  # noqa: F401,PLC0415
        return True
    except Exception:
        return False


def model_ready() -> bool:
    """True when weights exist on disk (they may be added at any time)."""
    return os.path.exists(weights_path())


def model_status() -> dict:
    path = weights_path()
    return {
        "available": vision_available(),
        "weights_found": os.path.exists(path),
        "weights_path": path,
        "device": detect_device(),
        "loaded": _model is not None,
        "error": MODEL_IMPORT_ERROR,
    }


def get_model():
    """Load YOLO weights on first use.

    Weights may be added after the server has started (the operator drops
    `yolov8x-fire.pt` into the weights volume), so this deliberately re-checks
    the filesystem instead of caching a failure.
    """
    global _model, _model_path, MODEL_IMPORT_ERROR
    path = weights_path()
    if _model is not None and _model_path == path:
        return _model
    with _model_lock:
        if _model is not None and _model_path == path:
            return _model
        if not os.path.exists(path):
            raise RuntimeError(
                f"Веса модели не найдены: {path}. Положите файл .pt в каталог weights/ "
                "или укажите YOLO_WEIGHTS / YOLO_WEIGHTS_URL."
            )
        expected = weights_sha256()
        if expected:
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(
                    "Контрольная сумма весов модели не совпадает с YOLO_WEIGHTS_SHA256 "
                    f"(ожидалось {expected[:16]}…, получено {actual[:16]}…). Загрузка отменена."
                )
        try:
            from ultralytics import YOLO  # noqa: PLC0415
        except Exception as exc:
            MODEL_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                "Библиотека ultralytics/torch не установлена, поэтому анализ изображений недоступен. "
                "Установите зависимости из backend/requirements.txt."
            ) from exc
        _model = YOLO(path)
        _model_path = path
        MODEL_IMPORT_ERROR = None
        log.info("YOLO model loaded from %s on %s", path, detect_device())
    return _model


def _resize_for_infer(frame):
    """Downscale large frames so YOLO runs faster with minimal accuracy loss."""
    max_w = int(os.getenv("YOLO_MAX_WIDTH") or 1280)
    h, w = frame.shape[:2]
    if w <= max_w:
        return frame
    scale = max_w / w
    return cv2.resize(frame, (max_w, int(h * scale)), interpolation=cv2.INTER_AREA)


def detect(frame):
    if not CV2_AVAILABLE:
        raise RuntimeError("OpenCV не установлен: анализ изображений недоступен")
    frame = _resize_for_infer(frame)
    half = (os.getenv("YOLO_HALF") or "true").strip().lower() == "true"
    if detect_device() == "cpu":
        half = False  # half precision is a CUDA-only optimisation
    r = get_model().predict(frame, device=detect_device(), half=half, verbose=False,
                            imgsz=int(os.getenv("YOLO_IMGSZ") or 640))[0]
    dets = [{"cls": r.names[int(b.cls)], "conf": round(float(b.conf), 4),
             "xyxy": [round(float(v), 1) for v in b.xyxy[0]]} for b in r.boxes]
    return max((d["conf"] for d in dets), default=0.0), dets, r.plot()


def analyze(path, is_video, fps=1.0, max_frames=None):
    """Most confident result: (conf, dets, raw_bgr, annotated_bgr). Videos/RTSP are sampled at `fps`."""
    if not CV2_AVAILABLE:
        raise RuntimeError("OpenCV не установлен: анализ изображений недоступен")
    if not is_video:
        frame = cv2.imread(path)
        if frame is None:
            return None
        c, d, ann = detect(frame)
        return c, d, frame, ann

    cap = cv2.VideoCapture(path)
    try:
        # Bound blocking reads on network streams (FFmpeg backend honours these).
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(os.getenv("RTSP_OPEN_TIMEOUT_MS") or 10000))
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(os.getenv("RTSP_READ_TIMEOUT_MS") or 10000))
        except Exception:
            pass
        deadline = time.monotonic() + float(os.getenv("STREAM_MAX_SECONDS") or 180)
        step = max(int(round((cap.get(cv2.CAP_PROP_FPS) or 25) / fps)), 1)
        best, i, n = None, 0, 0
        while True:
            if time.monotonic() > deadline:
                log.warning("stream analysis hit the %.0fs deadline; stopping", float(os.getenv("STREAM_MAX_SECONDS") or 180))
                break
            ok, frame = cap.read()
            if not ok:
                break
            if i % step == 0:
                c, d, ann = detect(frame)
                if best is None or c > best[0]:
                    best = (c, d, frame, ann)
                n += 1
                if max_frames and n >= max_frames:
                    break
            i += 1
        return best
    finally:
        cap.release()


# --------------------------------------------------------------------------
# image persistence: never keep originals at full weight
# --------------------------------------------------------------------------
def _encode_jpeg(img, max_width: int | None = None, quality: int | None = None) -> bytes | None:
    """Downscale and re-encode a frame as JPEG. Returns None if OpenCV is absent."""
    if not CV2_AVAILABLE or img is None:
        return None
    q = int(quality if quality is not None else (os.getenv("IMAGE_JPEG_QUALITY") or 82))
    limit = int(max_width if max_width is not None else (os.getenv("IMAGE_MAX_WIDTH") or 1600))
    if limit and img.shape[1] > limit:
        scale = limit / img.shape[1]
        img = cv2.resize(img, (limit, max(1, int(img.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
    return buf.tobytes() if ok else None


def save_image(path, img, max_width: int | None = None) -> bool:
    """Write a compressed JPEG. A 12 MP phone photo becomes a few hundred KB."""
    data = _encode_jpeg(img, max_width)
    if data is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def make_thumbnail(src_path, dst_path, width: int | None = None) -> bool:
    """Small preview for lists and popups (default 320 px)."""
    if not CV2_AVAILABLE:
        return False
    img = cv2.imread(str(src_path))
    if img is None:
        return False
    return save_image(dst_path, img, max_width=int(width if width is not None else
                                                  (os.getenv("THUMB_WIDTH") or 320)))


def exif_gps(path):
    """(lat, lon) from EXIF, or None. Never raises on malformed metadata."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(path) as img:
            g = img.getexif().get_ifd(0x8825)
            if not g:
                return None

            def dms(v, ref):
                deg, minutes, seconds = float(v[0]), float(v[1]), float(v[2])
                value = deg + minutes / 60 + seconds / 3600
                return -value if str(ref).upper() in ("S", "W") else value

            lat, lon = dms(g[2], g[1]), dms(g[4], g[3])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        if lat == 0 and lon == 0:
            return None
        return lat, lon
    except Exception as exc:
        log.debug("exif read failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------
UA = {"User-Agent": os.getenv("NOMINATIM_USER_AGENT") or "kedr-fire-monitor/1.0 (contact: admin@example.org)"}
OVERPASS = [u.strip() for u in (os.getenv("OVERPASS_URLS") or
                                "https://overpass-api.de/api/interpreter,"
                                "https://maps.mail.ru/osm/tools/overpass/api/interpreter").split(",") if u.strip()]
_overpass_sem = asyncio.Semaphore(int(os.getenv("OVERPASS_CONCURRENCY") or 4))
_client: httpx.AsyncClient | None = None
_client_lock = threading.Lock()


def http_client() -> httpx.AsyncClient:
    """One pooled client per process instead of a new TLS handshake per call."""
    global _client
    if _client is None or _client.is_closed:
        with _client_lock:
            if _client is None or _client.is_closed:
                _client = httpx.AsyncClient(
                    timeout=httpx.Timeout(15.0, connect=5.0),
                    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                    headers=UA, follow_redirects=True,
                )
    return _client


async def aclose_http() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# --------------------------------------------------------------------------
# tiny in-process TTL cache
# --------------------------------------------------------------------------
_CACHE: dict = {}
_CACHE_MAX = int(os.getenv("CACHE_MAX") or 4000)
_cache_order: list = []


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit is None:
        return None
    if hit[0] > time.time():
        return hit[1]
    _CACHE.pop(key, None)
    return None


def _cache_put(key, value, ttl):
    if key not in _CACHE and len(_CACHE) >= _CACHE_MAX:
        # Evict the oldest quarter without scanning the whole dict.
        now = time.time()
        expired = [k for k, v in list(_CACHE.items())[: _CACHE_MAX // 4] if v[0] <= now]
        for k in (expired or list(_CACHE)[: _CACHE_MAX // 4]):
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time() + ttl, value)


#: Per-mirror health, so a dead mirror is skipped instead of costing us a timeout
#: on every single incident.
_MIRROR_STATE: dict[str, dict] = {}


def _mirror_order() -> list[str]:
    now = time.time()
    def key(url: str):
        st = _MIRROR_STATE.get(url) or {}
        if st.get("until", 0) > now:
            return (2, st["until"])            # cooling down -> last
        return (1, st.get("latency", 0.0))     # healthy -> fastest recent first
    return sorted(OVERPASS, key=key)


def _all_mirrors_cooling() -> bool:
    now = time.time()
    return not any((_MIRROR_STATE.get(u) or {}).get("until", 0) <= now for u in OVERPASS)


async def overpass(query, key, ttl=900, timeout=None):
    """POST to Overpass with caching, mirror failover, cooldowns and a circuit breaker.

    Mirrors are tried one at a time (never raced: that is what gets public
    instances to ban clients). A mirror that fails goes into an exponential
    cooldown, so a broken instance costs one timeout and is then skipped. When
    every mirror is cooling down the call fails immediately and the caller falls
    back to the local database instead of waiting.
    """
    cached = _cache_get(key)
    if cached is not None:
        return cached
    recent_failure = _cache_get(f"neg:{key}")
    if recent_failure is not None:
        raise RuntimeError(f"Overpass недавно уже падал: {recent_failure}")

    async with _overpass_sem:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        if _all_mirrors_cooling():
            raise RuntimeError("Все зеркала Overpass на охлаждении после ошибок")

        t = timeout or OVERPASS_TIMEOUT
        last = "Overpass недоступен"
        for url in _mirror_order():
            st = _MIRROR_STATE.setdefault(url, {})
            if st.get("until", 0) > time.time():
                continue
            started = time.monotonic()
            try:
                r = await http_client().post(url, data={"data": query}, timeout=t)
                r.raise_for_status()
                data = r.json()
                st["fails"] = 0
                st["latency"] = time.monotonic() - started
                st["until"] = 0
                _cache_put(key, data, ttl)
                return data
            except Exception as exc:
                st["fails"] = st.get("fails", 0) + 1
                cooldown = min(int(os.getenv("OVERPASS_COOLDOWN_MAX") or 300),
                               (int(os.getenv("OVERPASS_COOLDOWN_BASE") or 30)) * st["fails"])
                st["until"] = time.time() + cooldown
                last = f"{type(exc).__name__}: {exc}"
                log.warning("overpass mirror %s failed (%s); cooldown %ss", url, last, cooldown)

        _cache_put(f"neg:{key}", last, int(os.getenv("OVERPASS_NEGATIVE_TTL") or 60))
        raise RuntimeError(last)


async def weather(lat, lon):
    """Real Open-Meteo data, or (None, reason). No invented defaults."""
    key = f"wx:{round(lat, 4)}:{round(lon, 4)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    err = ""
    for attempt in range(2):
        try:
            r = await http_client().get(
                os.getenv("OPEN_METEO_URL") or "https://api.open-meteo.com/v1/forecast",
                params={"latitude": round(lat, 5), "longitude": round(lon, 5), "wind_speed_unit": "ms",
                        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m"},
                timeout=10,
            )
            r.raise_for_status()
            w = r.json()["current"]
            out = ({"wind_speed_ms": w["wind_speed_10m"], "wind_dir_deg": w["wind_direction_10m"],
                    "temp_c": w["temperature_2m"], "humidity_pct": w["relative_humidity_2m"]}, "")
            _cache_put(key, out, int(os.getenv("WEATHER_TTL") or 600))
            return out
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                await asyncio.sleep(0.5)
    log.warning("weather unavailable: %s", err)
    return None, err


async def reverse_geocode(lat, lon):
    """(display_name, country_code) from Nominatim; ('', '') when unreachable."""
    key = f"geo:{round(lat, 4)}:{round(lon, 4)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        base_url = os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org/reverse"
        reverse_url = base_url if "/reverse" in base_url else base_url.rstrip("/") + "/reverse"
        r = await http_client().get(
            reverse_url,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 12, "accept-language": "ru"},
            timeout=8,
        )
        r.raise_for_status()
        j = r.json()
        out = (j.get("display_name", "") or "", (j.get("address", {}).get("country_code") or ""))
        _cache_put(key, out, int(os.getenv("GEOCODE_TTL") or 86400))
        return out
    except Exception as exc:
        log.warning("geocode unavailable: %s", exc)
        return "", ""


def _norm(s: str) -> str:
    """Normalization for comparison: lowercase, ё→е, collapse spaces."""
    return " ".join((s or "").lower().replace("ё", "е").split())


def _load_local_cities():
    """Load ru_cities.json once on first access."""
    path = Path(__file__).parent / "data" / "ru_cities.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)["cities"]


_local_cities = None
_local_cities_lock = threading.Lock()


def local_cities():
    global _local_cities
    if _local_cities is None:
        with _local_cities_lock:
            if _local_cities is None:
                _local_cities = _load_local_cities()
    return _local_cities


def local_city_search(q: str, limit: int = 8) -> list[dict]:
    """Instant search against the local dataset. Prefix > contains > english prefix, then population."""
    nq = _norm(q)
    if len(nq) < 2:
        return []
    hits = []
    for c in local_cities():
        name = _norm(c.get("n", ""))
        name_en = _norm(c.get("a", "") or "")
        if name.startswith(nq):
            rank = 0
        elif nq in name:
            rank = 1
        elif name_en.startswith(nq):
            rank = 2
        elif nq in name_en:
            rank = 3
        else:
            continue
        hits.append((rank, -(c.get("p") or 0), c))
    hits.sort(key=lambda x: (x[0], x[1]))
    return [
        {"name": f'{c["n"]}, {c.get("r", "")}'.strip(", "), "lat": c["lat"], "lon": c["lon"],
         "type": "city", "population": c.get("p"), "source": "local", "importance": 1.0}
        for _, _, c in hits[:limit]
    ]


def _nominatim_search_url() -> str:
    base = os.getenv("NOMINATIM_URL") or "https://nominatim.openstreetmap.org/reverse"
    return base.replace("/reverse", "/search") if "/reverse" in base else base.rstrip("/") + "/search"


async def nominatim_search(q: str, limit: int = 8, lang: str = "ru") -> list[dict]:
    """Nominatim search — settlements only, sorted by importance."""
    r = await http_client().get(
        _nominatim_search_url(),
        params={"q": q, "format": "jsonv2", "countrycodes": "ru", "limit": limit,
                "addressdetails": 1, "featuretype": "settlement", "accept-language": lang},
        headers={**UA, "Accept-Language": lang},
        timeout=6,
    )
    r.raise_for_status()
    allowed = {"city", "town", "village", "hamlet", "municipality", "administrative"}
    out = []
    for x in r.json():
        t = (x.get("type") or "").lower()
        if t not in allowed:
            continue
        try:
            out.append({"name": (x.get("display_name") or "").strip(), "lat": float(x["lat"]), "lon": float(x["lon"]),
                        "type": t, "population": None, "source": "osm",
                        "importance": float(x.get("importance", 0.0))})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda p: -p["importance"])
    return out[:limit]


async def nominatim_places(lat, lon, radius_km=20):
    """Bounded Nominatim search around a point — fallback when Overpass is down."""
    key = f"nomnear:{round(lat, 3)}:{round(lon, 3)}:{int(radius_km)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    dlat = radius_km / 111.0
    dlon = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-3)
    viewbox = f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}"
    rows = []
    try:
        r = await http_client().get(
            _nominatim_search_url(),
            params={"q": "город", "format": "jsonv2", "countrycodes": "ru", "limit": 20,
                    "viewbox": viewbox, "bounded": 1, "accept-language": "ru", "featuretype": "settlement"},
            timeout=8,
        )
        r.raise_for_status()
        for x in r.json():
            la, lo = float(x["lat"]), float(x["lon"])
            if haversine_km(lat, lon, la, lo) > radius_km:
                continue
            rows.append({"name": (x.get("display_name") or "?").split(", ")[0], "lat": la, "lon": lo,
                         "type": x.get("type") or "village", "population": None})
    except Exception as exc:
        log.warning("nominatim nearby failed: %s", exc)
    _cache_put(key, rows, int(os.getenv("GEOCODE_TTL") or 86400))
    return rows


def haversine_km(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


def bearing(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# --------------------------------------------------------------------------
# terrain + barriers (lightweight Overpass, no osmnx)
# --------------------------------------------------------------------------
_TERRAIN_SLOPE_DEG = {
    "forest": float(os.getenv("SLOPE_FOREST_DEG") or 8.0),
    "mixed": float(os.getenv("SLOPE_MIXED_DEG") or 5.0),
    "urban": float(os.getenv("SLOPE_URBAN_DEG") or 2.0),
    "field": float(os.getenv("SLOPE_FIELD_DEG") or 3.0),
    "water": 0.0,
}


def terrain_from_near(near: dict, fuel: str) -> str:
    """Terrain type derived from the land-cover data `nearby()` already fetched.

    This replaces the old per-incident `osmnx.features_from_point` call, which
    cost a full network round-trip for a value the model never used.
    """
    for w in (near or {}).get("landuse", []):
        kind = (w.get("kind") or "").lower()
        if kind in ("forest", "wood", "scrub"):
            return "forest"
        if kind in ("residential", "industrial", "commercial"):
            return "urban"
        if kind in ("farmland", "meadow", "grass", "grassland", "orchard", "vineyard"):
            return "field"
        if kind in ("water", "wetland"):
            return "water"
    return {"forest": "forest", "urban": "urban", "mixed": "field"}.get(fuel, "field")


def slope_deg_for_terrain(terrain: str) -> float:
    """Heuristic terrain slope (a real DEM is out of scope for this deployment)."""
    return _TERRAIN_SLOPE_DEG.get(terrain, 0.0)


_BARRIER_Q = """[out:json][timeout:{t}];
(
  way["highway"~"^(motorway|trunk|primary)$"](around:{r},{lat},{lon});
  way["waterway"="river"](around:{r},{lat},{lon});
);
out geom 60;"""


async def check_fire_barriers(lat, lon, radius_m, fire_polygon=None):
    """Roads/rivers that intersect the spread zone, via ONE cached Overpass query.

    Informational metadata only: barriers are reported to the operator but do not
    (yet) modify the computed fire polygon.
    """
    if (os.getenv("ENABLE_FIRE_BARRIERS") or "false").strip().lower() not in ("1", "true", "yes", "on"):
        return []
    radius_m = int(max(500, min(radius_m, 30000)))
    try:
        data = await overpass(_BARRIER_Q.format(t=int(min(OVERPASS_TIMEOUT, 20)), r=radius_m, lat=lat, lon=lon),
                              f"bar:{round(lat, 3)}:{round(lon, 3)}:{radius_m}",
                              ttl=int(os.getenv("BARRIER_TTL") or 86400))
    except Exception as exc:
        log.warning("barrier query failed: %s", exc)
        return []
    out = []
    for el in data.get("elements", []):
        geom = el.get("geometry") or []
        if len(geom) < 2:
            continue
        line = LineString([(p["lon"], p["lat"]) for p in geom if "lon" in p and "lat" in p])
        if line.is_empty:
            continue
        if fire_polygon is not None and not line.intersects(fire_polygon):
            continue
        tags = el.get("tags", {}) or {}
        kind = "river" if tags.get("waterway") == "river" else "road"
        out.append({"kind": kind, "subtype": tags.get("highway") or tags.get("waterway"),
                    "name": tags.get("name:ru") or tags.get("name") or "", "geometry": mapping(line)})
        if len(out) >= 40:
            break
    return out


# --------------------------------------------------------------------------
# nearby settlements + land cover: ONE Overpass call per query, cached
# --------------------------------------------------------------------------
_PLACE_RE = "^(city|town|village|hamlet)$"
_LANDUSE_RE = "^(forest|wood|residential|industrial|commercial|farmland|meadow|grass|orchard|vineyard)$"


async def nearby(lat, lon, radius_m=20000):
    """Settlements + land cover in ONE Overpass round-trip.

    Previously this was two queries, which doubled both our latency and the load
    on the public mirrors. A single union query returns the same elements.
    """
    radius_m = max(2000, min(int(radius_m), 30000))
    land_r = min(radius_m, 8000)
    t_sec = int(min(OVERPASS_TIMEOUT, 12))
    query = (
        f"[out:json][timeout:{t_sec}];("
        f'node["place"~"{_PLACE_RE}"](around:{radius_m},{lat},{lon});'
        f'way["landuse"~"{_LANDUSE_RE}"](around:{land_r},{lat},{lon});'
        f'way["natural"~"^(wood|scrub)$"](around:{land_r},{lat},{lon});'
        f");out tags center 600;"
    )
    try:
        data = await overpass(query, f"near:{round(lat, 3)}:{round(lon, 3)}:{radius_m}",
                              ttl=int(os.getenv("NEAR_TTL") or 1800))
    except Exception as exc:
        log.warning("nearby query failed: %s", exc)
        data = {"elements": []}

    places, landuse = [], []
    for el in data.get("elements", []):
        tg = el.get("tags", {}) or {}
        if el.get("type") == "node" and tg.get("place"):
            if "lat" not in el:
                continue
            pop = str(tg.get("population", "")).replace(" ", "")
            places.append({"name": tg.get("name:ru") or tg.get("name") or "?", "lat": el["lat"],
                           "lon": el["lon"], "type": tg.get("place"),
                           "population": int(pop) if pop.isdigit() else None})
        elif tg.get("landuse") or tg.get("natural"):
            c = el.get("center") or {}
            la, lo = el.get("lat", c.get("lat")), el.get("lon", c.get("lon"))
            if la is None or lo is None:
                continue
            landuse.append({"kind": tg.get("landuse") or tg.get("natural"), "lat": la, "lon": lo})

    src = "osm" if (places or landuse) else "none"
    if not places:
        extra = await nominatim_places(lat, lon, radius_km=radius_m / 1000)
        if extra:
            places, src = extra, "nominatim" if src == "none" else src
    return {"places": places, "landuse": landuse, "source": src}


_URBAN = {"residential", "industrial", "commercial"}
_FOREST = {"forest", "wood", "scrub", "grassland", "wetland"}


def select_places(poly, places, lat, lon, extra_km=30, buffer_km=1.0):
    """Priority: inside polygon → inside buffer (1 km from border) → nearest within extra_km."""
    if not places:
        return [], "none"
    if poly is None or poly.is_empty:
        zone = []
    else:
        try:
            padded = poly.buffer(buffer_km / 111.32)
        except Exception:
            padded = poly
        zone = [p for p in places if padded.covers(Point(p["lon"], p["lat"]))]
    if zone:
        zone.sort(key=lambda p: -(p.get("population") or 0))
        return zone[:40], "zone"
    near = []
    for p in places:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d <= extra_km:
            near.append((d, p))
    near.sort(key=lambda x: x[0])
    return [p for _, p in near[:15]], "nearby"


def classify_fuel(lat, lon, near):
    """City vs forest fuel zone, weighted by nearby OSM land cover and settlement size."""
    urban = forest = 0.0
    for w in (near or {}).get("landuse", []):
        d = haversine_km(lat, lon, w["lat"], w["lon"])
        weight = max(0.0, 1.0 - d / 8.0)
        if w["kind"] in _URBAN:
            urban += 2.0 * weight
        elif w["kind"] in _FOREST:
            forest += 2.0 * weight
    for p in (near or {}).get("places", []):
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d <= 3.0 and (p.get("type") in ("city", "town") or (p.get("population") or 0) >= 5000):
            urban += 2.5 * (1.0 - d / 3.0)
    if urban > forest * 1.15:
        return "urban"
    if forest > urban:
        return "forest"
    return "mixed"


# --------------------------------------------------------------------------
# Fire spread: physical model (see app/fire_model.py) + elliptical wavefront
# --------------------------------------------------------------------------
def burn_minutes_default() -> float:
    """Assumed un-suppressed burning time, minutes.

    This is a *scenario* input, not a fuel property: it says how long the fire is
    assumed to keep spreading before the zone is evaluated. It is the single most
    influential assumption on the zone size, so it is explicit, configurable
    (HAZARD_BURN_MINUTES) and reported back in the API.
    """
    return float(os.getenv("HAZARD_BURN_MINUTES") or 180.0)


def _ca_spread(lat, lon, ros_head, ecc, down_deg, burn_min, cell=100.0, fuel="forest", max_cells=400,
               initial_radius_m=0.0):
    """Huygens elliptical wavefront on an 8-connected grid.

    The radial law r(theta) = r_head * (1 - e) / (1 - e * cos(theta)) is the polar
    equation of an ellipse with the ignition point at the rear focus, which is the
    standard fire-shape assumption (Anderson 1983, used by FARSITE). The head, flank
    and backing rates therefore all follow from one ROS and one eccentricity.

    `initial_radius_m` seeds the already-burning patch: a fire is detected after it
    has an extent, and without it slow fuels discretise to a single cell.
    """
    half = ros_head * burn_min + 2 * cell + initial_radius_m
    n = max(int(half / cell) + 1, 2)
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat)) or 1.0
    ux = math.sin(math.radians(down_deg))
    uy = math.cos(math.radians(down_deg))
    W = 2 * n + 1
    c0 = n
    INF = float("inf")
    arrival = [INF] * (W * W)
    heap = []
    seed = initial_radius_m / cell if initial_radius_m > 0 else 0.0
    for xi in range(W):
        for yi in range(W):
            if seed and math.hypot(xi - c0, yi - c0) <= seed:
                arrival[xi * W + yi] = 0.0
                heap.append((0.0, xi, yi))
    if not heap:
        arrival[c0 * W + c0] = 0.0
        heap.append((0.0, c0, c0))
    heapq.heapify(heap)
    diag = cell * math.sqrt(2)

    while heap:
        d, x, y = heapq.heappop(heap)
        if d > arrival[x * W + y]:
            continue
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                xx, yy = x + dx, y + dy
                if xx < 0 or yy < 0 or xx >= W or yy >= W:
                    continue
                step = diag if (dx and dy) else cell
                cos = (dx * ux + dy * uy) / math.sqrt(dx * dx + dy * dy)
                # guard the denominator: e can approach 1 for strong wind
                ros = ros_head * (1 - ecc) / max(1 - ecc * cos, 1e-3)
                if ros <= 0:
                    continue
                nd = d + step / ros
                if nd < arrival[xx * W + yy] and nd <= burn_min:
                    arrival[xx * W + yy] = nd
                    heapq.heappush(heap, (nd, xx, yy))

    burned = [(xi, yi) for xi in range(W) for yi in range(W) if arrival[xi * W + yi] <= burn_min]
    if not burned:
        return Polygon(), [], 0

    cells_data = []
    step_n = max(1, len(burned) // max_cells) if max_cells else 1
    for idx, (xi, yi) in enumerate(burned):
        a = arrival[xi * W + yi]
        if idx % step_n == 0 and len(cells_data) < max_cells:
            y_off = (yi - c0) * cell
            x_off = (xi - c0) * cell
            cells_data.append({
                "lat": round(lat + y_off / m_lat, 6),
                "lon": round(lon + x_off / m_lon, 6),
                "intensity": round(1.0 - (a / burn_min), 3),
                "arrival_time": round(a, 1),
                "fuel": fuel,
            })

    boxes = []
    for xi, yi in burned:
        x = (xi - c0) * cell
        y = (yi - c0) * cell
        boxes.append(box(lon + (x - cell / 2) / m_lon, lat + (y - cell / 2) / m_lat,
                         lon + (x + cell / 2) / m_lon, lat + (y + cell / 2) / m_lat))
    hull = unary_union(boxes)
    return (hull if not hull.is_empty else Polygon()), cells_data, len(burned)


def _ellipse_poly(lat, lon, a_m, b_m, down_deg, off=0.0):
    th = math.radians(down_deg)
    ux, uy = math.sin(th), math.cos(th)
    px, py = uy, -ux
    m_lat = 111_320.0
    m_lon = 111_320.0 * math.cos(math.radians(lat)) or 1.0
    pts = []
    for k in range(72):
        t = 2 * math.pi * k / 72
        u, v = off + a_m * math.cos(t), b_m * math.sin(t)
        pts.append((lon + (u * ux + v * px) / m_lon, lat + (u * uy + v * py) / m_lat))
    return Polygon(pts)


def hazard_polygon(lat, lon, wind_ms, wind_from_deg, temp_c=None, humidity_pct=None,
                   fuel="forest", burn_minutes=None, slope_deg=None):
    """Spread zone from the physical model.

    Returns (polygon, cells, cell_count, diagnostics).
    """
    minutes = burn_minutes or burn_minutes_default()
    params = fm.spread_parameters(fuel, wind_ms, slope_deg, humidity_pct, temp_c)
    ros_head = params["ros_m_per_min"]
    ecc = params["eccentricity"] if (wind_ms is not None and wind_from_deg is not None) else 0.0
    down = (wind_from_deg + 180.0) % 360.0 if ecc else 0.0
    reach = ros_head * minutes

    # Adaptive resolution: enough cells across the reach to resolve the shape,
    # but never so many that a long burn explodes the grid.
    cell_min = float(os.getenv("CA_CELL_MIN_M") or 20.0)
    cell_max = float(os.getenv("CA_CELL_MAX_M") or 200.0)
    cell = max(cell_min, min(cell_max, reach / 40.0)) if reach > 0 else cell_min
    initial_radius = float(os.getenv("INITIAL_BURN_RADIUS_M") or 50.0)
    max_cells = int(os.getenv("HAZARD_CELLS_LIMIT") or 400)

    poly, cells_data, cell_count = _ca_spread(lat, lon, ros_head, ecc, down, minutes, cell=cell, fuel=fuel,
                                              max_cells=max_cells, initial_radius_m=initial_radius)
    # When the discretisation is too coarse to describe the shape — a tiny fire
    # whose extent is only a handful of cells — the analytic ellipse is both
    # smoother and closer to the model than a blocky union of grid squares.
    minx, miny, maxx, maxy = poly.bounds if not poly.is_empty else (0, 0, 0, 0)
    extent_m = max((maxx - minx) * 111320 * math.cos(math.radians(lat)), (maxy - miny) * 111320)
    too_coarse = cell_count <= 9 or extent_m < 8 * cell
    if poly.is_empty or poly.area == 0 or cell_count <= 1 or too_coarse:
        a = max(reach, initial_radius)
        b = a * max(1 - ecc, 0.25)
        poly = _ellipse_poly(lat, lon, a, b, down, off=0.35 * a if ecc else 0.0)
        if not cells_data and cell_count <= 1:
            cells_data = [{"lat": round(lat, 6), "lon": round(lon, 6), "intensity": 1.0,
                           "arrival_time": 0.0, "fuel": fuel}]
            cell_count = max(cell_count, 1)

    # Simplify proportionally to the zone: a fixed 50 m tolerance collapsed small
    # (slow-fuel) zones into a diamond. 5% of the extent, capped at 50 m.
    if not poly.is_empty:
        minx, miny, maxx, maxy = poly.bounds
        ext = max((maxx - minx) * 111320 * math.cos(math.radians(lat)), (maxy - miny) * 111320)
        tol_m = max(2.0, min(50.0, ext * 0.05))
        simplified = poly.simplify(tol_m / 111320.0, preserve_topology=True)
        poly = simplified if not simplified.is_empty else poly
    diagnostics = dict(params)
    diagnostics.update({"burn_minutes": round(minutes, 1), "cell_size_m": round(cell, 1),
                        "cell_count": cell_count})
    # Size of the burned zone: area, perimeter, longest chord (diameter) and the
    # farthest distance from the ignition point.
    diagnostics.update(fm.zone_metrics(poly, lat, lon))
    return poly, cells_data, cell_count, diagnostics


def evacuation_plan(poly, lat, lon, places, facilities, wind_from=None):
    """Settlements in the zone with a recommended evacuation bearing and nearest aid."""
    rows = []
    for p in places:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        evac = _evac_bearing(lat, lon, p["lat"], p["lon"], wind_from)
        nearby_fac = sorted(
            [f for f in facilities if f["kind"] in ("hospital", "fire_station", "helipad", "ambulance")],
            key=lambda f: haversine_km(p["lat"], p["lon"], f["lat"], f["lon"]),
        )[:2]
        rows.append({
            "name": p["name"], "type": p.get("type"), "population": p.get("population"),
            "lat": p["lat"], "lon": p["lon"], "distance_km": round(d, 1),
            "bearing": round(bearing(lat, lon, p["lat"], p["lon"])), "evac_bearing": round(evac),
            "nearest_aid": [{"kind": f["kind"], "name": f["name"],
                             "distance_km": round(haversine_km(p["lat"], p["lon"], f["lat"], f["lon"]), 1)}
                            for f in nearby_fac],
        })
    return sorted(rows, key=lambda r: r["distance_km"])[:40]


def _evac_bearing(lat, lon, place_lat, place_lon, wind_from=None):
    """Evacuation bearing: perpendicular to the fire's downwind axis."""
    brg = bearing(lat, lon, place_lat, place_lon)
    if wind_from is None:
        return brg
    down = (wind_from + 180) % 360
    rel = (brg - down + 540) % 360 - 180
    return (down + (90 if rel >= 0 else -90)) % 360


# --------------------------------------------------------------------------
# critical infrastructure near a point
# --------------------------------------------------------------------------
_INFRA_MEDICAL_Q = """[out:json][timeout:{t}];
(
  nwr["amenity"~"^(hospital|clinic|doctors|pharmacy)$"](around:{r},{lat},{lon});
  nwr["healthcare"~"^(hospital|clinic)$"](around:{r},{lat},{lon});
  nwr["emergency"="ambulance_station"](around:{r},{lat},{lon});
);
out tags center 300;"""

_INFRA_EMERGENCY_Q = """[out:json][timeout:{t}];
(
  nwr["amenity"~"^(fire_station|police|townhall)$"](around:{r},{lat},{lon});
  nwr["emergency"~"^(fire_station|shelter|water_tank|landing_site)$"](around:{r},{lat},{lon});
  nwr["military"="bunker"](around:{r},{lat},{lon});
);
out tags center 300;"""

_INFRA_CRITICAL_Q = """[out:json][timeout:{t}];
(
  nwr["power"="plant"]["plant:source"~"^(nuclear|hydro)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["industrial"~"^(refinery|chemical|oil|gas)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["product"~"^(oil|gas|chemical)$"](around:{r},{lat},{lon});
  nwr["hazard"~"^(chemical|nuclear|radiological|explosive)$"](around:{r},{lat},{lon});
);
out tags center 200;"""

_INFRA_HAZARD_Q = """[out:json][timeout:{t}];
(
  nwr["power"="plant"](around:{r},{lat},{lon});
  nwr["power"="substation"]["voltage"~"^(110000|220000|330000|500000|750000)$"](around:{r},{lat},{lon});
  nwr["aeroway"="aerodrome"](around:{r},{lat},{lon});
  nwr["aeroway"="helipad"](around:{r},{lat},{lon});
);
out tags center 300;"""

_CRITICALITY = {
    "nuclear": 5, "hydro_dam": 5, "refinery": 5,
    "hospital": 4, "fire_station": 4, "airport": 4,
    "power_plant": 4, "chemical": 4,
    "police": 3, "substation": 3, "helipad": 3, "shelter": 3,
    "ambulance": 3, "government": 3,
    "clinic": 2, "school": 2, "kindergarten": 2, "water": 2, "industrial": 2,
    "pharmacy": 1, "other": 1,
}


def infra_classify(tags: dict) -> tuple[str, int]:
    """Returns (kind, criticality)."""
    a = tags.get("amenity", "")
    em = tags.get("emergency", "")
    pw = tags.get("power", "")
    mw = tags.get("man_made", "")
    hc = tags.get("healthcare", "")
    ind = tags.get("industrial", "")
    haz = tags.get("hazard", "")
    src = (tags.get("plant:source") or tags.get("generator:source") or "").lower()
    prod = (tags.get("product") or "").lower()
    name = (tags.get("name") or "").upper()

    if pw == "plant" and ("nuclear" in src or "АЭС" in name):
        return "nuclear", _CRITICALITY["nuclear"]
    if pw == "plant" and ("hydro" in src or "ГЭС" in name):
        return "hydro_dam", _CRITICALITY["hydro_dam"]
    if ind == "refinery" or prod in ("oil", "gas") or "НПЗ" in name:
        return "refinery", _CRITICALITY["refinery"]
    if haz == "chemical" or ind == "chemical":
        return "chemical", _CRITICALITY["chemical"]
    if pw == "plant":
        return "power_plant", _CRITICALITY["power_plant"]
    if pw == "substation":
        return "substation", _CRITICALITY["substation"]

    if hc == "hospital" or a == "hospital":
        return "hospital", _CRITICALITY["hospital"]
    if hc == "clinic" or a in ("clinic", "doctors"):
        return "clinic", _CRITICALITY["clinic"]
    if em == "ambulance_station":
        return "ambulance", _CRITICALITY["ambulance"]
    if a == "pharmacy":
        return "pharmacy", _CRITICALITY["pharmacy"]

    if a == "fire_station" or em == "fire_station":
        return "fire_station", _CRITICALITY["fire_station"]
    if a == "police":
        return "police", _CRITICALITY["police"]
    if em == "shelter" or tags.get("military") == "bunker":
        return "shelter", _CRITICALITY["shelter"]

    if a == "townhall":
        return "government", _CRITICALITY["government"]
    if em == "water_tank" or mw == "water_tower":
        return "water", _CRITICALITY["water"]
    if tags.get("aeroway") == "aerodrome":
        return "airport", _CRITICALITY["airport"]
    if tags.get("aeroway") == "helipad" or em == "landing_site":
        return "helipad", _CRITICALITY["helipad"]

    if a == "school":
        return "school", _CRITICALITY["school"]
    if a == "kindergarten":
        return "kindergarten", _CRITICALITY["kindergarten"]
    if mw == "works":
        return "industrial", _CRITICALITY["industrial"]

    return "other", _CRITICALITY["other"]


def _to_int(v):
    if v is None:
        return None
    try:
        return int(str(v).replace(" ", "").split(";")[0])
    except (ValueError, TypeError):
        return None


#: One union query instead of four: fewer round-trips to a public service and
#: a much smaller chance of hitting its rate limits.
_INFRA_Q = """[out:json][timeout:{t}];
(
  nwr["amenity"~"^(hospital|clinic|doctors|pharmacy|fire_station|police|townhall)$"](around:{r},{lat},{lon});
  nwr["healthcare"~"^(hospital|clinic)$"](around:{r},{lat},{lon});
  nwr["emergency"~"^(ambulance_station|fire_station|shelter|water_tank|landing_site)$"](around:{r},{lat},{lon});
  nwr["power"="plant"](around:{r},{lat},{lon});
  nwr["power"="substation"]["voltage"~"^(110000|220000|330000|500000|750000)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["industrial"~"^(refinery|chemical|oil|gas)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["product"~"^(oil|gas|chemical)$"](around:{r},{lat},{lon});
  nwr["hazard"~"^(chemical|nuclear|radiological|explosive)$"](around:{r},{lat},{lon});
  nwr["aeroway"~"^(aerodrome|helipad)$"](around:{r},{lat},{lon});
  nwr["military"="bunker"](around:{r},{lat},{lon});
);
out tags center 800;"""

_CRITICAL_KINDS = {"nuclear", "hydro_dam", "refinery", "chemical"}


def _allowed_radius(kind: str, radius_m: int, critical_radius_m: int, hazard_radius_m: int) -> int:
    """Each category keeps its own search radius even though one query fetched them."""
    if kind in _CRITICAL_KINDS:
        return max(radius_m, critical_radius_m)
    if kind in ("power_plant", "substation", "airport", "helipad"):
        return max(radius_m, hazard_radius_m)
    return radius_m


async def infrastructure_near(lat, lon, radius_m=15000, hazard_radius_m=50000):
    """Critical infrastructure near a point, from a single Overpass query.

    Critical objects (nuclear/hydro/refinery/chemical) are searched in
    CRITICAL_RADIUS_M, ordinary energy/transport objects in HAZARD_RADIUS_M and
    medical/emergency services in `radius_m`. One query is issued at the largest
    radius and the per-category radii are applied locally, which is equivalent to
    four separate queries but costs one round-trip.
    """
    radius_m = max(2000, min(int(radius_m), 30000))
    hazard_radius_m = max(radius_m, min(int(hazard_radius_m), 80000))
    critical_radius_m = max(radius_m, min(int(os.getenv("CRITICAL_RADIUS_M") or 30000), 80000))
    fetch_r = max(radius_m, hazard_radius_m, critical_radius_m)
    t = int(OVERPASS_TIMEOUT)

    try:
        data = await overpass(_INFRA_Q.format(t=t, r=fetch_r, lat=lat, lon=lon),
                              key=f"infra:{round(lat, 3)}:{round(lon, 3)}:{fetch_r}",
                              ttl=int(os.getenv("INFRA_TTL") or 1800), timeout=t + 5)
        source = "osm"
    except Exception as exc:
        log.warning("infra query failed: %s", exc)
        return [], "none"

    seen_ids, rows = set(), []
    for el in data.get("elements", []):
        oid = (el.get("type"), el.get("id"))
        if oid in seen_ids:
            continue
        seen_ids.add(oid)
        tg = el.get("tags", {}) or {}
        c = el.get("center") or {}
        la, lo = el.get("lat", c.get("lat")), el.get("lon", c.get("lon"))
        if la is None or lo is None:
            continue
        kind, crit = infra_classify(tg)
        if kind == "other":
            continue
        d = haversine_km(lat, lon, la, lo)
        if d * 1000 > _allowed_radius(kind, radius_m, critical_radius_m, hazard_radius_m):
            continue
        rows.append({
            "kind": kind, "criticality": crit,
            "name": tg.get("name:ru") or tg.get("name") or tg.get("operator") or "?",
            "lat": la, "lon": lo, "distance_km": round(d, 2),
            "beds": _to_int(tg.get("beds")), "capacity": _to_int(tg.get("capacity")),
            "emergency": tg.get("emergency") in ("yes", "designated"),
            "phone": tg.get("phone") or tg.get("contact:phone"),
            "voltage": tg.get("voltage"), "plant_source": tg.get("plant:source"),
            "operator": tg.get("operator"),
        })

    rows.sort(key=lambda r: (-r["criticality"], r["distance_km"]))
    return rows[:120], source


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_font_registered: str | None = None


def _resolve_font() -> str | None:
    global _font_registered
    if _font_registered:
        return _font_registered
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for candidate in _FONT_CANDIDATES:
        if os.path.exists(candidate):
            try:
                pdfmetrics.registerFont(TTFont("KedrSans", candidate))
                _font_registered = "KedrSans"
                return _font_registered
            except Exception as exc:
                log.warning("font %s could not be registered: %s", candidate, exc)
    return None


def make_pdf(i, uploads) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as RLImage
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _resolve_font() or "Helvetica"
    ss = getSampleStyleSheet()
    for k in ("Normal", "Title", "Heading2"):
        ss[k].fontName = font
    style = TableStyle([("FONTNAME", (0, 0), (-1, -1), font), ("FONTSIZE", (0, 0), (-1, -1), 9),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey)])
    props = i.hazard_geojson.get("properties", {}) if isinstance(i.hazard_geojson, dict) else {}
    fuel = {"forest": "лес", "urban": "город/посёлок", "mixed": "смешанная"}.get(props.get("fuel"), "—")
    info = [["GPS", f"{i.lat:.6f}, {i.lon:.6f}"], ["Местность", i.region_name or "—"],
            ["Источник координат", i.gps_source.value if i.gps_source else "—"],
            ["Топливо (зона)", fuel],
            ["Время сканирования (UTC)", i.created_at.strftime("%Y-%m-%d %H:%M:%S")],
            ["Уверенность ИИ", f"{i.confidence * 100:.1f}%"],
            ["Ветер", f"{i.wind_speed_ms} м/с, {i.wind_dir_deg}°" if i.wind_speed_ms is not None else "нет данных"],
            ["Температура / влажность", f"{i.temp_c} °C / {i.humidity_pct} %" if i.temp_c is not None else "нет данных"]]
    if props.get("max_criticality", 0) >= 4:
        info.append(["Критические объекты", f"Уровень опасности: {props.get('max_criticality', 0)}"])
    if i.evacuation and isinstance(i.evacuation[0], dict) and i.evacuation[0].get("nearest_aid"):
        aid = ", ".join(f"{a['kind']}: {a['name']} ({a['distance_km']} км)" for a in i.evacuation[0]["nearest_aid"][:2])
        info.append(["Ближайшая помощь", aid])

    # Fire-behaviour block from the physical model (Rothermel / Byram).
    if props.get("ros_m_per_min") is not None:
        info += [["Топливная модель", str(props.get("fuel_model", "—"))],
                 ["Влажность топлива", f"{props.get('fuel_moisture', 0) * 100:.1f} %"],
                 ["Скорость распространения", f"{props.get('ros_m_per_min')} м/мин "
                                              f"(ветер в приземном слое {props.get('wind_midflame_ms')} м/с)"],
                 ["Интенсивность / пламя", f"{props.get('fireline_intensity_kw_m')} кВт/м "
                                           f"({props.get('intensity_class')}), пламя {props.get('flame_length_m')} м"],
                 ["Окно расчёта", f"{props.get('burn_minutes')} мин"],
                 ["Размер очага", f"{props.get('diameter_m')} м в диаметре, "
                                  f"{props.get('area_ha')} га, периметр {props.get('perimeter_km')} км"]]

    ev = [["Населённый пункт", "Население", "Расстояние, км", "Азимут, °", "Эвакуация, °"]] + \
         [[str(r.get("name", "")), r.get("population") if r.get("population") is not None else "н/д",
           r.get("distance_km", ""), r.get("bearing", ""), r.get("evac_bearing", "")]
          for r in (i.evacuation or []) if isinstance(r, dict) and "distance_km" in r]

    story = [Paragraph(f"Донесение о лесном пожаре № {i.id}", ss["Title"])]
    img = (uploads / i.annotated_path) if i.annotated_path else None
    if img and img.exists():
        try:
            story += [RLImage(str(img), width=170 * mm, height=95 * mm, kind="proportional"), Spacer(1, 6 * mm)]
        except Exception as exc:
            log.warning("pdf image could not be embedded: %s", exc)
    story += [Table(info, colWidths=[60 * mm, 110 * mm], style=style), Spacer(1, 4 * mm)]
    if i.weather_fallback:
        story.append(Paragraph("Метеоданные недоступны на момент формирования отчёта.", ss["Normal"]))
    story += [Paragraph("Таблица эвакуации", ss["Heading2"]),
              Table(ev if len(ev) > 1 else ev + [["Угрожаемых пунктов нет", "", "", "", ""]], style=style)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm).build(story)
    return buf.getvalue()
