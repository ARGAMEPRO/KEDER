"""Detection, EXIF, weather, geocoding, hazard spread (Rothermel + cellular automata), evacuation, PDF."""
import asyncio
import heapq
import io
import json
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

import cv2
import httpx
from PIL import Image
from shapely.geometry import MultiPoint, Point, Polygon, LineString, mapping

try:
    import osmnx as ox
    import geopandas as gpd
    OSMNX_AVAILABLE = True
except ImportError:
    OSMNX_AVAILABLE = False
    print("[kedr] osmnx/geopandas not available, using basic OSM queries")

_model = None
_model_lock = threading.Lock()
DEFAULT_WX = dict(wind_speed_ms=3.0, wind_dir_deg=270.0, temp_c=15.0, humidity_pct=50.0)


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                path = os.getenv("YOLO_WEIGHTS", "weights/yolov8x-fire.pt")
                if not os.path.exists(path):
                    raise RuntimeError(f"Model weights not found at {path}. Train first: docker compose --profile train run --rm trainer")
                from ultralytics import YOLO
                _model = YOLO(path)
    return _model


def _resize_for_infer(frame):
    """Downscale large frames so YOLO runs faster with minimal accuracy loss."""
    max_w = int(os.getenv("YOLO_MAX_WIDTH", 1280))
    h, w = frame.shape[:2]
    if w <= max_w:
        return frame
    scale = max_w / w
    return cv2.resize(frame, (max_w, int(h * scale)), interpolation=cv2.INTER_AREA)


def detect(frame):
    frame = _resize_for_infer(frame)
    r = get_model().predict(frame, device=os.getenv("YOLO_DEVICE", "cuda:0"),
                            half=os.getenv("YOLO_HALF", "true").lower() == "true", verbose=False,
                            imgsz=int(os.getenv("YOLO_IMGSZ", 640)))[0]
    dets = [{"cls": r.names[int(b.cls)], "conf": round(float(b.conf), 4),
             "xyxy": [round(float(v), 1) for v in b.xyxy[0]]} for b in r.boxes]
    return max((d["conf"] for d in dets), default=0.0), dets, r.plot()


def analyze(path, is_video, fps=1.0, max_frames=None):
    """Most confident result: (conf, dets, raw_bgr, annotated_bgr). Videos/RTSP are sampled at `fps`."""
    if not is_video:
        frame = cv2.imread(path)
        if frame is None:
            return None
        c, d, ann = detect(frame)
        return c, d, frame, ann
    cap = cv2.VideoCapture(path)
    try:
        step = max(int(round((cap.get(cv2.CAP_PROP_FPS) or 25) / fps)), 1)
        best, i, n = None, 0, 0
        while True:
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


def exif_gps(path):
    try:
        with Image.open(path) as img:
            g = img.getexif().get_ifd(0x8825)
            if not g:
                return None
            dms = lambda v, ref: (float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600) * (-1 if ref in "SW" else 1)
            return dms(g[2], g[1]), dms(g[4], g[3])
    except Exception:
        return None


UA = {"User-Agent": os.getenv("NOMINATIM_USER_AGENT", "kedr-fire-monitor/1.0 (contact: your-email@example.org)")}
# Public Overpass mirrors, tried in order. `overpass-api.de` is the reference instance.
OVERPASS = [u.strip() for u in os.getenv(
    "OVERPASS_URLS",
    "https://overpass-api.de/api/interpreter,"
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
).split(",") if u.strip()]
OVERPASS_TIMEOUT = float(os.getenv("OVERPASS_TIMEOUT", 10))
_overpass_sem = asyncio.Semaphore(int(os.getenv("OVERPASS_CONCURRENCY", 8)))
_search_cache = {}

# ---------- tiny in-process TTL cache: keeps slow external calls off the hot path ----------
_CACHE: dict = {}
_CACHE_MAX = int(os.getenv("CACHE_MAX", 4000))


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    if hit:
        _CACHE.pop(key, None)
    return None


def _cache_put(key, value, ttl):
    if len(_CACHE) >= _CACHE_MAX:
        now = time.time()
        for k in [k for k, v in _CACHE.items() if v[0] <= now] or list(_CACHE)[: _CACHE_MAX // 4]:
            _CACHE.pop(k, None)
    _CACHE[key] = (time.time() + ttl, value)


async def overpass(query, key, ttl=900, timeout=None):
    """POST Overpass: cache hit, then race public mirrors and take the first 200."""
    cached = _cache_get(key)
    if cached is not None:
        print(f"[kedr] overpass cache hit for key: {key[:50]}...")
        return cached
    async with _overpass_sem:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        t = timeout or OVERPASS_TIMEOUT
        print(f"[kedr] overpass query timeout: {t}s, mirrors: {len(OVERPASS)}")

        async def one(url):
            try:
                async with httpx.AsyncClient(timeout=t) as c:
                    r = await c.post(url, data={"data": query}, headers=UA)
                    r.raise_for_status()
                    data = r.json()
                    print(f"[kedr] overpass success from {url}")
                    return data
            except Exception as e:
                print(f"[kedr] overpass error from {url}: {e}")
                raise

        tasks = [asyncio.create_task(one(url)) for url in OVERPASS]
        last = "Overpass unavailable"
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    data = await fut
                    _cache_put(key, data, ttl)
                    return data
                except Exception as e:
                    last = f"{type(e).__name__}: {e}"
                    print(f"[kedr] overpass {last}", flush=True)
        finally:
            for task in tasks:
                task.cancel()
        print(f"[kedr] overpass failed for all mirrors: {last}")
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
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(os.getenv("OPEN_METEO_URL", "https://api.open-meteo.com/v1/forecast"), params={
                    "latitude": round(lat, 5), "longitude": round(lon, 5), "wind_speed_unit": "ms",
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m"})
                r.raise_for_status()
                w = r.json()["current"]
            out = (dict(wind_speed_ms=w["wind_speed_10m"], wind_dir_deg=w["wind_direction_10m"],
                        temp_c=w["temperature_2m"], humidity_pct=w["relative_humidity_2m"]), "")
            _cache_put(key, out, int(os.getenv("WEATHER_TTL", 600)))
            return out
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(0.5 * (attempt + 1))
    print(f"[kedr] weather unavailable: {err}", flush=True)
    return None, err


async def reverse_geocode(lat, lon):
    """(display_name, country_code) from Nominatim; ('', '') if unreachable. Cached for a day."""
    key = f"geo:{round(lat, 4)}:{round(lon, 4)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        base_url = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
        reverse_url = base_url if "/reverse" in base_url else base_url.rstrip("/") + "/reverse"
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(reverse_url,
                            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 12, "accept-language": "ru"},
                            headers=UA)
            r.raise_for_status()
            j = r.json()
        out = (j.get("display_name", ""), (j.get("address", {}).get("country_code") or ""))
        _cache_put(key, out, int(os.getenv("GEOCODE_TTL", 86400)))
        return out
    except Exception as e:
        print(f"[kedr] geocode unavailable: {e}", flush=True)
        return "", ""


def _norm(s: str) -> str:
    """Normalization for comparison: lowercase, ё→е, collapse spaces."""
    return " ".join(s.lower().replace("ё", "е").split())


@lru_cache(maxsize=1)
def _load_local_cities():
    """Load ru_cities.json once on first access."""
    path = Path(__file__).parent / "data" / "ru_cities.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["cities"]


def local_city_search(q: str, limit: int = 8) -> list[dict]:
    """Instant search against local dataset. Ranking: prefix > contains > eng-prefix, then by population."""
    nq = _norm(q)
    if len(nq) < 2:
        return []
    cities = _load_local_cities()
    hits = []
    for c in cities:
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
        {
            "name": f'{c["n"]}, {c.get("r", "")}'.strip(", "),
            "lat": c["lat"],
            "lon": c["lon"],
            "type": "city",
            "population": c.get("p"),
            "source": "local",
            "importance": 1.0,
        }
        for _, _, c in hits[:limit]
    ]


async def nominatim_search(q: str, limit: int = 8, lang: str = "ru") -> list[dict]:
    """Nominatim search — settlements only, sorted by importance."""
    base = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
    url = base.replace("/reverse", "/search")
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.get(
            url,
            params={
                "q": q,
                "format": "jsonv2",
                "countrycodes": "ru",
                "limit": limit,
                "addressdetails": 1,
                "featuretype": "settlement",
                "accept-language": lang,
            },
            headers={**UA, "Accept-Language": lang},
        )
        r.raise_for_status()
    allowed = {"city", "town", "village", "hamlet", "municipality", "administrative"}
    out = []
    for x in r.json():
        t = (x.get("type") or "").lower()
        if t not in allowed:
            continue
        out.append({
            "name": x.get("display_name", "").strip(),
            "lat": float(x["lat"]),
            "lon": float(x["lon"]),
            "type": t,
            "population": None,
            "source": "osm",
            "importance": float(x.get("importance", 0.0)),
        })
    out.sort(key=lambda p: -p["importance"])
    return out[:limit]


async def search_places(q):
    """Search any city/town/village in Russia via Nominatim (cached)."""
    key = f"search:{q.strip().lower()}"
    cached = _cache_get(key)
    if cached is not None:
        print(f"[kedr] search cache hit: {q}")
        return cached
    base_url = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
    search_url = base_url.replace("/reverse", "/search") if "/reverse" in base_url else base_url.rsplit("/", 1)[0] + "/search"
    print(f"[kedr] search URL: {search_url}, query: {q}")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(search_url,
                            params={"q": q, "format": "jsonv2", "countrycodes": "ru", "limit": 8, "accept-language": "ru"},
                            headers=UA)
            r.raise_for_status()
        data = r.json()
        print(f"[kedr] search results count: {len(data)}")
        seen, res = set(), []
        for x in data:
            k = (x["display_name"].split(", ")[0], round(float(x["lat"])), round(float(x["lon"])))
            if k not in seen:
                seen.add(k)
                res.append({"name": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"]), "type": x.get("type", "")})
        _cache_put(key, res, int(os.getenv("SEARCH_TTL", 3600)))
        return res
    except Exception as e:
        print(f"[kedr] search error: {e}")
        raise


async def nominatim_places(lat, lon, radius_km=20):
    """Bounded Nominatim search around a point — fallback when Overpass is down."""
    key = f"nomnear:{round(lat, 3)}:{round(lon, 3)}:{int(radius_km)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    dlat = radius_km / 111.0
    dlon = radius_km / max(111.0 * math.cos(math.radians(lat)), 1e-3)
    west, east = lon - dlon, lon + dlon
    south, north = lat - dlat, lat + dlat
    viewbox = f"{west},{north},{east},{south}"
    base_url = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
    url = base_url.replace("/reverse", "/search") if "/reverse" in base_url else base_url.rsplit("/", 1)[0] + "/search"
    rows = []
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(url, params={
                "q": "город", "format": "jsonv2", "countrycodes": "ru", "limit": 20,
                "viewbox": viewbox, "bounded": 1, "accept-language": "ru",
                "featuretype": "settlement",
            }, headers=UA)
            r.raise_for_status()
            for x in r.json():
                la, lo = float(x["lat"]), float(x["lon"])
                if haversine_km(lat, lon, la, lo) > radius_km:
                    continue
                rows.append({"name": x.get("display_name", "?").split(", ")[0], "lat": la, "lon": lo,
                             "type": x.get("type") or "village", "population": None})
    except Exception as e:
        print(f"[kedr] nominatim nearby failed: {e}", flush=True)
    _cache_put(key, rows, int(os.getenv("GEOCODE_TTL", 86400)))
    return rows


def haversine_km(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def bearing(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


# ---------- settlements + land cover: ONE Overpass call per point, cached ----------
_PLACE_RE = "^(city|town|village|hamlet)$"
_LANDUSE_RE = "^(forest|wood|residential|industrial|commercial|farmland|meadow|grass|orchard|vineyard)$"


def get_terrain_type(lat: float, lon: float) -> str:
    """
    Определяет тип местности по координатам точки с помощью OpenStreetMap.
    Использует osmnx если доступен, иначе использует базовый OSM запрос.
    """
    if not OSMNX_AVAILABLE:
        return "field"  # Fallback to basic classification

    try:
        point = Point(lon, lat)  # Shapely порядок (долгота, широта)
        tags = {"landuse": True, "natural": True, "leisure": True, "waterway": True}
        gdf = ox.features_from_point((lat, lon), tags=tags, dist=100)

        if gdf.empty:
            return "field"  # По умолчанию

        for _, row in gdf.iterrows():
            if row.geometry.contains(point):
                if row.get("natural") == "wood" or row.get("landuse") == "forest":
                    return "forest"
                elif row.get("landuse") in ["residential", "commercial", "industrial", "retail"]:
                    return "urban"
                elif row.get("landuse") in ["farmland", "meadow", "grass"]:
                    return "field"
                elif row.get("waterway") or row.get("natural") == "water":
                    return "water"

    except Exception as e:
        print(f"[kedr] Ошибка запроса OSM terrain: {e}")

    return "field"  # Дефолтное значение при сбое


def check_fire_barriers(center_lat, center_lon, radius_meters, fire_polygon):
    """
    Загружает дороги и реки из OSM и проверяет, блокируют ли они распространение огня.
    """
    if not OSMNX_AVAILABLE:
        return []

    try:
        # Загружаем дороги
        roads = ox.graph_from_point((center_lat, center_lon), dist=radius_meters, network_type="drive")
        road_edges = ox.graph_to_gdfs(roads, nodes=False, edges=True)

        # Загружаем реки
        waterways = ox.features_from_point((center_lat, center_lon), tags={"waterway": "river"}, dist=radius_meters)

        blocked_directions = []

        # Проверяем пересечения с дорогами
        for _, road in road_edges.iterrows():
            if road.geometry.intersects(fire_polygon):
                highway_type = road.get("highway", "")
                if highway_type in ["motorway", "trunk", "primary"]:
                    blocked_directions.append(("road", mapping(road.geometry), highway_type))

        # Проверяем реки
        if not waterways.empty:
            for _, water in waterways.iterrows():
                if water.geometry.intersects(fire_polygon):
                    blocked_directions.append(("river", mapping(water.geometry), "river"))

        return blocked_directions

    except Exception as e:
        print(f"[kedr] Ошибка проверки барьеров: {e}")
        return []


async def nearby(lat, lon, radius_m=20000):
    """Settlements (nodes, fast) plus optional land-cover. Places still return if landuse times out."""
    radius_m = max(2000, min(int(radius_m), 30000))
    t_sec = int(min(OVERPASS_TIMEOUT, 12))
    print(f"[kedr] nearby query: lat={lat}, lon={lon}, radius={radius_m}")
    q_places = (f'[out:json][timeout:{t_sec}];'
                f'node["place"~"{_PLACE_RE}"](around:{radius_m},{lat},{lon});out body 400;')
    q_land = (f'[out:json][timeout:{t_sec}];('
              f'way["landuse"~"{_LANDUSE_RE}"](around:{min(radius_m, 8000)},{lat},{lon});'
              f'way["natural"~"^(wood|scrub)$"](around:{min(radius_m, 8000)},{lat},{lon});'
              f');out tags center 80;')

    async def places_job():
        print(f"[kedr] places_job: executing query")
        try:
            data = await overpass(q_places, f"plc:{round(lat, 3)}:{round(lon, 3)}:{radius_m}",
                                  ttl=int(os.getenv("NEAR_TTL", 1800)))
            print(f"[kedr] places_job: got {len(data.get('elements', []))} elements")
        except Exception as e:
            print(f"[kedr] places_job error: {e}")
            return []
        rows = []
        for el in data.get("elements", []):
            tg = el.get("tags", {})
            if not tg.get("place") or "lat" not in el:
                continue
            pop = tg.get("population", "").replace(" ", "")
            rows.append({"name": tg.get("name:ru") or tg.get("name") or "?", "lat": el["lat"], "lon": el["lon"],
                         "type": tg.get("place"), "population": int(pop) if pop.isdigit() else None})
        print(f"[kedr] places_job: returning {len(rows)} places")
        return rows

    async def land_job():
        try:
            print(f"[kedr] land_job: executing query")
            data = await overpass(q_land, f"lu:{round(lat, 3)}:{round(lon, 3)}:{min(radius_m, 8000)}",
                                  ttl=int(os.getenv("NEAR_TTL", 1800)))
            print(f"[kedr] land_job: got {len(data.get('elements', []))} elements")
        except Exception as e:
            print(f"[kedr] land_job error: {e}")
            return []
        rows = []
        for el in data.get("elements", []):
            tg = el.get("tags", {})
            c = el.get("center") or {}
            la, lo = el.get("lat", c.get("lat")), el.get("lon", c.get("lon"))
            if la is None or lo is None:
                continue
            if tg.get("landuse") or tg.get("natural"):
                rows.append({"kind": tg.get("landuse") or tg.get("natural"), "lat": la, "lon": lo})
        print(f"[kedr] land_job: returning {len(rows)} landuse entries")
        return rows

    places, landuse = [], []
    src = "none"
    try:
        places, landuse = await asyncio.gather(places_job(), land_job())
        src = "osm" if (places or landuse) else "none"
    except Exception as e:
        print(f"[kedr] nearby overpass failed: {e}", flush=True)
        try:
            landuse = await land_job()
        except Exception:
            landuse = []
    if not places:
        extra = await nominatim_places(lat, lon, radius_km=radius_m / 1000)
        if extra:
            places, src = extra, "nominatim" if src == "none" else src
    return {"places": places, "landuse": landuse, "source": src}


_URBAN = {"residential", "industrial", "commercial"}
_FOREST = {"forest", "wood", "scrub", "grassland", "wetland"}


def select_places(poly, places, lat, lon, extra_km=30, buffer_km=1.0):
    """
    Priority: inside polygon → inside buffer (1 km from border) → nearest within extra_km.
    Returns (list, selection_mode).
    """
    if not places:
        return [], "none"

    if poly is None or poly.is_empty:
        zone = []
    else:
        try:
            # Convert 1km buffer to degrees (approximately)
            # 1 degree latitude ≈ 111.32 km
            buffer_deg = buffer_km / 111.32
            padded = poly.buffer(buffer_deg)  # ~1 km
        except Exception:
            padded = poly
        zone = [p for p in places if padded.covers(Point(p["lon"], p["lat"]))]

    if zone:
        # sort by population: large cities first
        zone.sort(key=lambda p: -(p.get("population") or 0))
        return zone[:40], "zone"

    # nobody in zone — take nearest
    near = []
    for p in places:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        if d <= extra_km:
            near.append((d, p))
    near.sort(key=lambda x: x[0])
    return [p for _, p in near[:15]], "nearby"


def classify_fuel(lat, lon, near):
    """Item 6: city vs forest fuel zone, weighted by nearby OSM land cover and settlement size."""
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


# ---------- Rothermel-style rates + cellular-automata spread ----------
_FUEL = {
    "forest": {"ros": 38.0, "wind_gain": 1.00, "burn": 200,
               "IR": 200.0, "xi": 0.1, "rho_b": 0.5, "epsilon": 0.9, "Qig": 250.0},
    "mixed":  {"ros": 30.0, "wind_gain": 0.95, "burn": 170,
               "IR": 150.0, "xi": 0.08, "rho_b": 0.6, "epsilon": 0.85, "Qig": 300.0},
    "urban":  {"ros": 20.0, "wind_gain": 0.55, "burn": 140,
               "IR": 100.0, "xi": 0.05, "rho_b": 0.8, "epsilon": 0.7, "Qig": 400.0},
}


def _moisture_factor(humidity_pct):
    h = 50.0 if humidity_pct is None else max(0.0, min(float(humidity_pct), 100.0))
    return max(0.35, 1 - h / 100 * 0.6)


def _temp_factor(temp_c):
    if temp_c is None or temp_c <= 25:
        return 1.0
    return 1 + min((temp_c - 25) * 0.02, 0.25)


def _rothermel_phi_w(wind_ms, gain=1.0):
    """Wind correction phi_w: exponential growth of the head rate of spread with wind speed."""
    if wind_ms is None or wind_ms <= 0:
        return 0.0
    return min(0.12 * (wind_ms ** 1.35) * gain, 2.8)


def _rothermel_phi_s(slope_deg):
    """Slope correction phi_s: terrain slope influence on fire spread."""
    if slope_deg is None or slope_deg <= 0:
        return 0.0
    # Simplified slope correction based on empirical data
    return min(5.275 * (slope_deg ** 1.5) / (slope_deg + 0.5), 3.0)


def rothermel_ros(fuel_type, wind_ms=None, slope_deg=None, humidity_pct=None, temp_c=None):
    """
    Calculate Rate of Spread (ROS) using Rothermel model:
    ROS = (IR * ξ * (1 + φw + φs)) / (ρb * ε * Qig)
    """
    model = _FUEL.get(fuel_type, _FUEL["forest"])
    IR = model["IR"]
    xi = model["xi"]
    rho_b = model["rho_b"]
    epsilon = model["epsilon"]
    Qig = model["Qig"]

    # Apply environmental factors
    phi_w = _rothermel_phi_w(wind_ms)
    phi_s = _rothermel_phi_s(slope_deg)
    moisture = _moisture_factor(humidity_pct)
    temp = _temp_factor(temp_c)

    # Calculate base ROS
    ros = (IR * xi * (1 + phi_w + phi_s)) / (rho_b * epsilon * Qig)

    # Apply environmental corrections
    ros *= moisture * temp

    # Convert to m/min (typical fire spread units)
    ros *= 60  # Convert from m/s to m/min

    return max(0.1, ros)  # Minimum spread rate


def cellular_automata_pburn(fuel_type, wind_ms=None, wind_dir_deg=None, 
                            slope_deg=None, base_flammability=0.3):
    """
    Calculate probability of ignition using cellular automata model:
    Pburn = P0 * (1 + Kwind * cos(θ)) * (1 + Kslope)
    """
    model = _FUEL.get(fuel_type, _FUEL["forest"])
    P0 = base_flammability * model["ros"] / 38.0  # Normalize by forest ROS

    # Wind factor
    Kwind = 0.0
    if wind_ms is not None and wind_ms > 0:
        Kwind = min(0.8 * (wind_ms / 10.0), 0.8)  # Saturates at 10 m/s

    # Slope factor
    Kslope = 0.0
    if slope_deg is not None and slope_deg > 0:
        Kslope = min(0.5 * (slope_deg / 30.0), 0.5)  # Saturates at 30 degrees

    # For general probability without direction, use maximum wind effect
    wind_factor = 1 + Kwind

    p_burn = P0 * wind_factor * (1 + Kslope)
    return max(0.0, min(1.0, p_burn))  # Clamp between 0 and 1


def cellular_automata_directional_pburn(fuel_type, from_lat, from_lon, to_lat, to_lon,
                                       wind_ms=None, wind_dir_deg=None, slope_deg=None,
                                       base_flammability=0.3):
    """
    Calculate directional probability of ignition with wind direction:
    Pburn = P0 * (1 + Kwind * cos(θ)) * (1 + Kslope)
    where θ is angle between wind vector and direction to neighbor cell
    """
    model = _FUEL.get(fuel_type, _FUEL["forest"])
    P0 = base_flammability * model["ros"] / 38.0

    # Calculate direction from fire to neighbor
    direction_deg = bearing(from_lat, from_lon, to_lat, to_lon)

    # Wind factor with direction
    Kwind = 0.0
    if wind_ms is not None and wind_ms > 0 and wind_dir_deg is not None:
        Kwind = min(0.8 * (wind_ms / 10.0), 0.8)
        # Wind direction is where wind is coming FROM, spread direction is where it goes TO
        wind_spread_dir = (wind_dir_deg + 180) % 360
        angle_diff = math.radians(direction_deg - wind_spread_dir)
        cos_theta = math.cos(angle_diff)
        wind_factor = 1 + Kwind * max(0, cos_theta)  # Only downwind spread
    else:
        wind_factor = 1.0

    # Slope factor
    Kslope = 0.0
    if slope_deg is not None and slope_deg > 0:
        Kslope = min(0.5 * (slope_deg / 30.0), 0.5)

    p_burn = P0 * wind_factor * (1 + Kslope)
    return max(0.0, min(1.0, p_burn))


def _ca_spread(lat, lon, ros_head, ecc, down_deg, burn_min, cell=100.0, fuel="forest"):
    """Cellular-automaton / Huygens wavefront on an 8-connected grid with colored visualization.

    Each burning cell ignites its neighbours after step / ROS(theta); ROS uses an
    elliptical Rothermel-style law, so the head, flank and backing fronts differ.
    Returns the convex hull of the cells that burn within `burn_min` minutes,
    plus cell-level intensity data for visualization.
    """
    half = ros_head * burn_min + 2 * cell
    n = max(int(half / cell) + 1, 2)
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat)) or 1.0
    ux = math.sin(math.radians(down_deg))
    uy = math.cos(math.radians(down_deg))
    W = 2 * n + 1
    c0 = n
    INF = float("inf")
    arrival = [INF] * (W * W)
    arrival[c0 * W + c0] = 0.0
    heap = [(0.0, c0, c0)]
    diag = cell * math.sqrt(2)

    # Calculate cell-level burn probabilities
    cells_data = []

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
                ros = ros_head * (1 - ecc) / max(1 - ecc * cos, 0.05)
                nd = d + step / ros
                if nd < arrival[xx * W + yy] and nd <= burn_min:
                    arrival[xx * W + yy] = nd
                    heapq.heappush(heap, (nd, xx, yy))

    burned = set()
    for xi in range(W):
        for yi in range(W):
            if arrival[xi * W + yi] <= burn_min:
                burned.add((xi, yi))
                # Calculate cell intensity for visualization
                intensity = 1.0 - (arrival[xi * W + yi] / burn_min)
                # Convert grid coordinates to lat/lon (center of cell)
                cell_lat_center = lat + ((yi - c0) * cell) / m_lat
                cell_lon_center = lon + ((xi - c0) * cell) / m_lon
                
                # Create square polygon for this cell
                half_cell = cell / 2
                cell_lat_min = lat + ((yi - c0) * cell - half_cell) / m_lat
                cell_lat_max = lat + ((yi - c0) * cell + half_cell) / m_lat
                cell_lon_min = lon + ((xi - c0) * cell - half_cell) / m_lon
                cell_lon_max = lon + ((xi - c0) * cell + half_cell) / m_lon
                
                cells_data.append({
                    "lat": cell_lat_center,
                    "lon": cell_lon_center,
                    "intensity": intensity,
                    "arrival_time": arrival[xi * W + yi],
                    "fuel": fuel,
                    "polygon": [
                        [cell_lon_min, cell_lat_min],
                        [cell_lon_max, cell_lat_min],
                        [cell_lon_max, cell_lat_max],
                        [cell_lon_min, cell_lat_max],
                        [cell_lon_min, cell_lat_min]
                    ]
                })

    pts = []
    for xi, yi in burned:  # only the outer boundary keeps the hull small and the shape smooth
        if all((xi + dx, yi + dy) in burned for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
            continue
        x = (xi - c0) * cell
        y = (yi - c0) * cell
        for ox in (-cell / 2, cell / 2):
            for oy in (-cell / 2, cell / 2):
                pts.append((lon + (x + ox) / m_lon, lat + (y + oy) / m_lat))
    if len(pts) < 3:
        return Polygon(), cells_data
    hull = MultiPoint(pts).convex_hull
    return hull if not hull.is_empty else Polygon(), cells_data


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


def hazard_polygon(lat, lon, wind_ms, wind_from_deg, temp_c=None, humidity_pct=None, fuel="forest", burn_minutes=None, slope_deg=None):
    """Rothermel + CA spread with cell-level intensity data for visualization."""
    model = _FUEL.get(fuel, _FUEL["forest"])
    minutes = burn_minutes or model["burn"]

    # Use proper Rothermel ROS calculation
    ros_head = rothermel_ros(fuel, wind_ms, slope_deg, humidity_pct, temp_c)

    known = wind_ms is not None and wind_from_deg is not None
    if known:
        # Eccentricity based on wind speed (simplified)
        ecc = min(0.9, 0.35 + 0.06 * wind_ms)
        down = (wind_from_deg + 180) % 360
    else:
        ecc, down = 0.0, 0.0

    poly, cells_data = _ca_spread(lat, lon, ros_head, ecc, down, minutes, fuel=fuel)
    if poly.is_empty or poly.area == 0:
        a = ros_head * minutes
        b = a * max(1 - ecc, 0.25)
        poly = _ellipse_poly(lat, lon, a, b, down, off=0.35 * a if known else 0.0)

    return poly, cells_data


def evacuation(poly, lat, lon, places, wind_from=None):
    """Settlements already selected for the zone, with recommended evacuation bearing."""
    rows = []
    for p in places:
        brg = bearing(lat, lon, p["lat"], p["lon"])
        if wind_from is None:
            evac = brg
        else:
            down = (wind_from + 180) % 360
            rel = (brg - down + 540) % 360 - 180
            evac = (down + (90 if rel >= 0 else -90)) % 360
        rows.append({"name": p["name"], "type": p.get("type"), "population": p.get("population"),
                     "distance_km": round(haversine_km(lat, lon, p["lat"], p["lon"]), 1),
                     "bearing": round(brg), "evac_bearing": round(evac)})
    return sorted(rows, key=lambda r: r["distance_km"])[:40]


def evacuation_plan(poly, lat, lon, places, facilities, wind_from=None):
    """
    Extended evacuation: considers not only settlements but also critical infrastructure.
    Returns list of evacuation points sorted by priority, with nearest medical facilities.
    """
    rows = []
    for p in places:
        d = haversine_km(lat, lon, p["lat"], p["lon"])
        # basic evacuation sideways from fire
        evac = _evac_bearing(lat, lon, p["lat"], p["lon"], wind_from)

        # nearest hospital/fire station/helipad for this settlement
        nearby_fac = sorted(
            [f for f in facilities if f["kind"] in ("hospital", "fire_station", "helipad")],
            key=lambda f: haversine_km(p["lat"], p["lon"], f["lat"], f["lon"]),
        )[:2]

        rows.append({
            "name": p["name"],
            "type": p.get("type"),
            "population": p.get("population"),
            "lat": p["lat"], "lon": p["lon"],
            "distance_km": round(d, 1),
            "bearing": round(bearing(lat, lon, p["lat"], p["lon"])),
            "evac_bearing": round(evac),
            "nearest_aid": [
                {"kind": f["kind"], "name": f["name"],
                 "distance_km": round(haversine_km(p["lat"], p["lon"], f["lat"], f["lon"]), 1)}
                for f in nearby_fac
            ],
        })
    return sorted(rows, key=lambda r: r["distance_km"])[:40]


def _evac_bearing(lat, lon, place_lat, place_lon, wind_from=None):
    """Calculate optimal evacuation bearing considering wind direction."""
    brg = bearing(lat, lon, place_lat, place_lon)
    if wind_from is None:
        return brg
    down = (wind_from + 180) % 360
    rel = (brg - down + 540) % 360 - 180
    return (down + (90 if rel >= 0 else -90)) % 360


# ---------- critical infrastructure near a point (item 7: MCHS view) ----------
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

# Energy and hazardous objects — SPECIAL radius, 50 km
_INFRA_HAZARD_Q = """[out:json][timeout:{t}];
(
  nwr["power"="plant"](around:{r},{lat},{lon});
  nwr["power"="substation"]["voltage"~"^(110000|220000|330000|500000|750000)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["industrial"~"^(refinery|chemical|oil|gas)$"](around:{r},{lat},{lon});
  nwr["man_made"="works"]["product"~"^(oil|gas|chemical)$"](around:{r},{lat},{lon});
  nwr["hazard"~"^(chemical|nuclear|radiological|explosive)$"](around:{r},{lat},{lon});
  nwr["aeroway"="aerodrome"](around:{r},{lat},{lon});
  nwr["aeroway"="helipad"](around:{r},{lat},{lon});
);
out tags center 300;"""


# Criticality level: 5 = evacuate entire zone, 1 = reference only
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
    """Returns (kind, criticality). Extended version of infra_kind."""
    a   = tags.get("amenity", "")
    em  = tags.get("emergency", "")
    pw  = tags.get("power", "")
    mw  = tags.get("man_made", "")
    hc  = tags.get("healthcare", "")
    ind = tags.get("industrial", "")
    haz = tags.get("hazard", "")
    src = (tags.get("plant:source") or tags.get("generator:source") or "").lower()
    prod = (tags.get("product") or "").lower()
    name = (tags.get("name") or "").upper()

    # --- Nuclear power plant ---
    if pw == "plant" and ("nuclear" in src or "АЭС" in name):
        return "nuclear", _CRITICALITY["nuclear"]
    # --- Hydroelectric dam ---
    if pw == "plant" and ("hydro" in src or "ГЭС" in name):
        return "hydro_dam", _CRITICALITY["hydro_dam"]
    # --- Oil refinery ---
    if ind == "refinery" or prod in ("oil", "gas") or "НПЗ" in name:
        return "refinery", _CRITICALITY["refinery"]
    # --- Chemical plant ---
    if haz == "chemical" or ind == "chemical":
        return "chemical", _CRITICALITY["chemical"]
    # --- Other power plants (thermal, gas) ---
    if pw == "plant":
        return "power_plant", _CRITICALITY["power_plant"]
    if pw == "substation":
        return "substation", _CRITICALITY["substation"]

    # --- Medical ---
    if hc == "hospital" or a == "hospital":
        return "hospital", _CRITICALITY["hospital"]
    if hc == "clinic" or a in ("clinic", "doctors"):
        return "clinic", _CRITICALITY["clinic"]
    if em == "ambulance_station":
        return "ambulance", _CRITICALITY["ambulance"]
    if a == "pharmacy":
        return "pharmacy", _CRITICALITY["pharmacy"]

    # --- Emergency services ---
    if a == "fire_station" or em == "fire_station":
        return "fire_station", _CRITICALITY["fire_station"]
    if a == "police":
        return "police", _CRITICALITY["police"]
    if em == "shelter" or tags.get("military") == "bunker":
        return "shelter", _CRITICALITY["shelter"]

    # --- Government / water / transport ---
    if a == "townhall":
        return "government", _CRITICALITY["government"]
    if em == "water_tank" or mw == "water_tower":
        return "water", _CRITICALITY["water"]
    if tags.get("aeroway") == "aerodrome":
        return "airport", _CRITICALITY["airport"]
    if tags.get("aeroway") == "helipad" or em == "landing_site":
        return "helipad", _CRITICALITY["helipad"]

    # --- Education / industrial ---
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


def infra_kind(tg):
    """Map OSM tags to a single infrastructure category. Uses new classification system."""
    kind, _ = infra_classify(tg)
    return kind


async def infrastructure_near(lat, lon, radius_m=15000, hazard_radius_m=50000):
    """
    Critical infrastructure: 3 parallel Overpass queries,
    deduplication by (kind, rounded coordinates), sorting by criticality ↓, distance ↑.
    """
    radius_m = max(2000, min(int(radius_m), 30000))
    hazard_radius_m = max(radius_m, min(int(hazard_radius_m), 80000))
    t = int(OVERPASS_TIMEOUT)

    async def q(query, key, r, ttl=1800):
        return await overpass(
            query.format(t=t, r=r, lat=lat, lon=lon),
            key=f"{key}:{round(lat,3)}:{round(lon,3)}:{r}",
            ttl=ttl,
            timeout=t + 5,
        )

    # three parallel queries
    results = await asyncio.gather(
        q(_INFRA_MEDICAL_Q,   "infra-med",  radius_m),
        q(_INFRA_EMERGENCY_Q, "infra-em",   radius_m),
        q(_INFRA_HAZARD_Q,    "infra-haz",  hazard_radius_m),
        return_exceptions=True,
    )

    seen_ids = set()
    rows = []
    sources_ok = 0
    for res in results:
        if isinstance(res, Exception):
            print(f"[kedr] infra overpass failed: {res}", flush=True)
            continue
        sources_ok += 1
        for el in res.get("elements", []):
            oid = (el.get("type"), el.get("id"))
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            tg = el.get("tags", {}) or {}
            c = el.get("center") or {}
            la = el.get("lat", c.get("lat"))
            lo = el.get("lon", c.get("lon"))
            if la is None or lo is None:
                continue

            kind, crit = infra_classify(tg)
            if kind == "other":
                continue

            d = haversine_km(lat, lon, la, lo)
            rows.append({
                "kind": kind,
                "criticality": crit,
                "name": tg.get("name:ru") or tg.get("name") or tg.get("operator") or "?",
                "lat": la, "lon": lo,
                "distance_km": round(d, 2),
                # useful details
                "beds": _to_int(tg.get("beds")),
                "capacity": _to_int(tg.get("capacity")),
                "emergency": tg.get("emergency") == "yes" or tg.get("emergency") == "designated",
                "phone": tg.get("phone") or tg.get("contact:phone"),
                "voltage": tg.get("voltage"),
                "plant_source": tg.get("plant:source"),
                "operator": tg.get("operator"),
            })

    # sorting: criticality DESC, distance ASC
    rows.sort(key=lambda r: (-r["criticality"], r["distance_km"]))

    # Return source status: osm (all good), partial (some failed), none (all failed)
    if sources_ok == 3:
        src = "osm"
    elif sources_ok > 0:
        src = "partial"
    else:
        src = "none"
    return rows[:120], src


def make_pdf(i, uploads) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    ss = getSampleStyleSheet()
    for k in ("Normal", "Title", "Heading2"):
        ss[k].fontName = "DejaVu"
    style = TableStyle([("FONTNAME", (0, 0), (-1, -1), "DejaVu"), ("FONTSIZE", (0, 0), (-1, -1), 9),
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
    ev = [["Населённый пункт", "Население", "Расстояние, км", "Азимут, °", "Эвакуация, °"]] + \
         [[r["name"], r["population"] if r.get("population") is not None else "н/д", r["distance_km"], r["bearing"], r.get("evac_bearing", "")] for r in i.evacuation if "distance_km" in r]

    # Add critical infrastructure summary if available
    if i.hazard_geojson and isinstance(i.hazard_geojson, dict):
        props = i.hazard_geojson.get("properties", {})
        if props.get("max_criticality", 0) >= 4:
            info.append(["Критические объекты", f"Уровень опасности: {props.get('max_criticality', 0)}"])

    # Add nearest aid information if available in evacuation data
    if i.evacuation and len(i.evacuation) > 0 and "nearest_aid" in i.evacuation[0]:
        nearest = i.evacuation[0].get("nearest_aid", [])
        if nearest:
            aid_info = ", ".join([f"{a['kind']}: {a['name']} ({a['distance_km']} км)" for a in nearest[:2]])
            info.append(["Ближайшая помощь", aid_info])
    story = [Paragraph(f"Донесение о лесном пожаре № {i.id}", ss["Title"])]
    img = uploads / i.annotated_path if i.annotated_path else None
    if img and img.exists():
        story += [RLImage(str(img), width=170 * mm, height=95 * mm, kind="proportional"), Spacer(1, 6 * mm)]
    story += [Table(info, colWidths=[60 * mm, 110 * mm], style=style), Spacer(1, 4 * mm)]
    if i.weather_fallback:
        story.append(Paragraph("Метеоданные недоступны на момент формирования отчёта.", ss["Normal"]))
    story += [Paragraph("Таблица эвакуации", ss["Heading2"]),
              Table(ev if len(ev) > 1 else ev + [["Угрожаемых пунктов нет", "", "", "", ""]], style=style)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm).build(story)
    return buf.getvalue()
