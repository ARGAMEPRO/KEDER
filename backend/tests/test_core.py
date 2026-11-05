"""Pure-logic tests: fire model, escalation tiers, geo helpers, security primitives."""
from __future__ import annotations

import math
import time

import pytest
from fastapi import HTTPException
from shapely.geometry import Point

from app import config, security
from app import services as sv
from app.main import _assert_safe_stream_url, _safe_upload_path, client_ip
from app.models import Status, classify, classify_detections


# ------------------------------------------------------------------ tiers
def test_tiers_fire():
    assert classify(0.39) == Status.CLEAR
    assert classify(0.40) == Status.PENDING_VERIFICATION
    assert classify(0.80) == Status.CRITICAL_ALERT


def test_tiers_smoke_are_stricter():
    assert classify(0.85, "smoke") == Status.PENDING_VERIFICATION
    assert classify(0.85, "fire") == Status.CRITICAL_ALERT
    assert classify(0.95, "smoke") == Status.CRITICAL_ALERT
    assert classify(0.5, "smoke") == Status.CLEAR


def test_classify_detections_uses_strongest_class_aware_verdict():
    assert classify_detections(0.85, [{"cls": "smoke", "conf": 0.85}]) == Status.PENDING_VERIFICATION
    assert classify_detections(0.85, [{"cls": "smoke", "conf": 0.85}, {"cls": "fire", "conf": 0.81}]) \
        == Status.CRITICAL_ALERT
    assert classify_detections(0.7, []) == Status.PENDING_VERIFICATION


# ------------------------------------------------------------------ fire physics
def test_simard_emc_is_monotonic_and_bounded():
    from app import fire_model as fm
    assert 0.01 <= fm.emc_simard(20, 35) < fm.emc_simard(70, 20) < fm.emc_simard(95, 10) <= 0.40
    # clamping must not produce nonsense outside the physical range
    assert 0.01 <= fm.emc_simard(-5, 60) <= 0.40
    assert 0.01 <= fm.emc_simard(500, -50) <= 0.40


def test_rothermel_matches_reference_magnitude():
    """FM8 at 8% moisture and no wind is ~0.4 ft/min in BehavePlus."""
    from app import fire_model as fm
    ros_ft = fm.rothermel_ros("timber_litter", 0.0, 0.0, 0.08) * fm.FT_PER_M
    assert 0.2 < ros_ft < 0.8, ros_ft


def test_fuel_ordering_is_physical():
    """Grass spreads fastest, built-up surface fuel slowest."""
    from app import fire_model as fm
    def ros(f):
        return fm.spread_parameters(f, 8.0, 8.0, 30.0, 30.0)["ros_m_per_min"]
    assert ros("field") > ros("mixed") > ros("forest") > ros("urban")


def test_wind_and_slope_and_dryness_increase_spread():
    from app import fire_model as fm
    assert fm.spread_parameters("forest", 15.0, 0, 40, 25)["ros_m_per_min"] > \
           fm.spread_parameters("forest", 2.0, 0, 40, 25)["ros_m_per_min"]
    assert fm.spread_parameters("forest", 5.0, 25, 40, 25)["ros_m_per_min"] > \
           fm.spread_parameters("forest", 5.0, 0, 40, 25)["ros_m_per_min"]
    assert fm.spread_parameters("forest", 5.0, 0, 15, 30)["ros_m_per_min"] > \
           fm.spread_parameters("forest", 5.0, 0, 95, 10)["ros_m_per_min"]


def test_midflame_wind_is_sheltered_in_forest():
    from app import fire_model as fm
    assert fm.midflame_wind(10.0, "forest") < fm.midflame_wind(10.0, "field")
    assert fm.midflame_wind(10.0, "forest") > 0
    assert fm.midflame_wind(None, "forest") == 0


def test_length_to_breadth_and_eccentricity():
    from app import fire_model as fm
    lbs = [fm.length_to_breadth(u) for u in (0, 1, 3, 8)]
    assert lbs == sorted(lbs)
    assert max(fm.length_to_breadth(u) for u in range(40)) <= fm.MAX_LB
    assert fm.eccentricity_from_lb(1.0) == 0.0
    e = fm.eccentricity_from_lb(3.0)
    assert 0 < e < 1
    # the ellipse relation must round-trip
    assert abs(1.0 / math.sqrt(1 - e * e) - 3.0) < 1e-6


def test_byram_intensity_and_flame_length():
    from app import fire_model as fm
    assert fm.byram_intensity("forest", 10.0) > fm.byram_intensity("forest", 2.0)
    assert fm.flame_length_m(10000) > fm.flame_length_m(1000)
    assert fm.intensity_class(500) == "low"
    assert fm.intensity_class(2000) == "moderate"
    assert fm.intensity_class(6000) == "high"
    assert fm.intensity_class(20000) == "extreme"


def test_spread_parameters_are_complete():
    from app import fire_model as fm
    p = fm.spread_parameters("forest", 8.0, 8.0, 30.0, 30.0)
    for key in ("fuel_model", "fuel_moisture", "ros_m_per_min", "length_to_breadth", "eccentricity",
                "fireline_intensity_kw_m", "flame_length_m", "intensity_class", "wind_midflame_ms",
                "continuity_factor"):
        assert key in p, key


# ------------------------------------------------------------------ spread geometry
def test_ellipse_follows_wind():
    poly, _cells, _n, _d = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0)
    assert poly.contains(Point(92.9, 56.0))
    minx, _miny, maxx, _maxy = poly.bounds
    assert (maxx - 92.9) > 3 * (92.9 - minx)


def test_circle_without_wind():
    poly, _cells, _n, _d = sv.hazard_polygon(56.0, 92.9, None, None)
    minx, _, maxx, _ = poly.bounds
    assert abs((maxx - 92.9) - (92.9 - minx)) < 1e-3


def test_humidity_and_temp_shrink_the_zone():
    dry, _, _, _ = sv.hazard_polygon(56.0, 92.9, 5.0, 270.0, temp_c=33, humidity_pct=15)
    wet, _, _, _ = sv.hazard_polygon(56.0, 92.9, 5.0, 270.0, temp_c=10, humidity_pct=90)
    assert (dry.bounds[2] - dry.bounds[0]) > 2 * (wet.bounds[2] - wet.bounds[0])


def test_urban_fuel_is_smaller_than_forest():
    urban, _, _, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0, fuel="urban")
    forest, _, _, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0, fuel="forest")
    assert (urban.bounds[2] - urban.bounds[0]) < 0.85 * (forest.bounds[2] - forest.bounds[0])


def test_cells_are_capped():
    _poly, cells, _count, diag = sv.hazard_polygon(56.0, 92.9, 20.0, 270.0, temp_c=40, humidity_pct=0)
    assert diag["cell_count"] >= len(cells)
    assert len(cells) <= int(__import__("os").getenv("HAZARD_CELLS_LIMIT") or 400)


def test_slow_fuel_still_produces_a_real_zone():
    """A single-cell zone made evacuation plans useless for slow fuels."""
    _poly, cells, count, _d = sv.hazard_polygon(56.0, 92.9, 6.0, 270.0, temp_c=24, humidity_pct=35,
                                                fuel="urban", slope_deg=2.0)
    assert count > 1
    # the point is that the zone is not degenerate, i.e. it has a real size
    assert _d["diameter_m"] > 60, f"degenerate zone: {_d['diameter_m']:.0f} m across"
    assert _d["area_ha"] > 0.01, _d
    assert _d["perimeter_km"] > 0
    assert cells


# ------------------------------------------------------------------ fuel / terrain
def test_fuel_classification():
    city = {"places": [{"name": "C", "lat": 56.01, "lon": 92.9, "type": "city", "population": 900000}],
            "landuse": [{"kind": "residential", "lat": 56.005, "lon": 92.9}]}
    forest = {"places": [], "landuse": [{"kind": "forest", "lat": 56.01, "lon": 92.9}]}
    assert sv.classify_fuel(56.0, 92.9, city) == "urban"
    assert sv.classify_fuel(56.0, 92.9, forest) == "forest"
    assert sv.classify_fuel(56.0, 92.9, {"places": [], "landuse": []}) == "mixed"


def test_terrain_derived_from_landuse():
    assert sv.terrain_from_near({"landuse": [{"kind": "forest", "lat": 1, "lon": 1}]}, "mixed") == "forest"
    assert sv.terrain_from_near({"landuse": [{"kind": "residential", "lat": 1, "lon": 1}]}, "forest") == "urban"
    assert sv.terrain_from_near({"landuse": []}, "forest") == "forest"
    assert sv.slope_deg_for_terrain("urban") < sv.slope_deg_for_terrain("forest")


def test_select_places_and_evacuation_direction():
    poly, _, _, _ = sv.hazard_polygon(56.0, 92.9, 8.0, 270.0)
    places = [{"name": "Inside", "lat": 56.0, "lon": 92.905, "population": 1000, "type": "town"},
              {"name": "Far", "lat": 55.0, "lon": 92.9, "population": 10, "type": "village"}]
    selected, mode = sv.select_places(poly, places, 56.0, 92.9, extra_km=0)
    assert mode == "zone" and [p["name"] for p in selected] == ["Inside"]

    rows = sv.evacuation_plan(poly, 56.0, 92.9, places, [], 270.0)
    assert rows[0]["name"] == "Inside"
    assert rows[0]["evac_bearing"] == 0  # perpendicular to the west->east fire axis


# ------------------------------------------------------------------ infra
def test_infra_classify():
    assert sv.infra_classify({"power": "plant", "plant:source": "nuclear"}) == ("nuclear", 5)
    assert sv.infra_classify({"amenity": "hospital"}) == ("hospital", 4)
    assert sv.infra_classify({"amenity": "pharmacy"}) == ("pharmacy", 1)
    assert sv.infra_classify({"amenity": "unknown"}) == ("other", 1)
    assert sv.infra_classify({"emergency": "fire_station"}) == ("fire_station", 4)


def test_haversine_and_bearing():
    assert abs(sv.haversine_km(55.7558, 37.6173, 59.9343, 30.3351) - 635) < 15
    assert abs(sv.bearing(56.0, 92.9, 56.0, 93.0) - 90) < 1
    assert abs(sv.bearing(56.0, 92.9, 56.1, 92.9) - 0) < 1


# ------------------------------------------------------------------ security
def test_media_tokens_roundtrip():
    token = security.sign_media_token("images/a.jpg", ttl_seconds=60)
    assert security.verify_media_token("images/a.jpg", token)
    assert not security.verify_media_token("images/b.jpg", token)      # bound to the path
    assert not security.verify_media_token("images/a.jpg", "garbage")
    assert not security.verify_media_token("images/a.jpg", "")
    expired = f"{int(time.time()) - 10}.{token.split('.', 1)[1]}"
    assert not security.verify_media_token("images/a.jpg", expired)


def test_password_policy():
    for good in ("GoodPass1", "пароль123", "aaa11111"):
        security.validate_password(good)
    for bad in ("short1", "nodigits", "12345678", "a" * 80):
        with pytest.raises(security.PasswordPolicyError):
            security.validate_password(bad)


def test_jwt_roundtrip_and_tampering():
    token = security.create_access_token(42, token_version=3)
    assert security.user_id_from_token(token) == (42, 3)
    assert security.user_id_from_token(token + "x") == (None, -1)
    assert security.user_id_from_token("") == (None, -1)


def test_weak_secret_rejected_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    for weak in ("", "change-me-in-production", "dev-secret-change-me", "short"):
        monkeypatch.setenv("SECRET_KEY", weak)
        with pytest.raises(RuntimeError):
            config.resolve_secret_key()
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert config.resolve_secret_key() == "x" * 40


def test_dev_secret_is_stable_and_never_the_placeholder(monkeypatch, tmp_path):
    """A weak dev key must be replaced by a generated one that survives restarts."""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SECRET_KEY", "change-me-in-production")
    monkeypatch.setenv("DEV_SECRET_FILE", str(tmp_path / "dev.key"))
    first = config.resolve_secret_key()
    second = config.resolve_secret_key()
    assert first == second, "the development key must be stable across restarts"
    assert len(first) >= config.MIN_SECRET_LEN
    assert first != "change-me-in-production"
    assert (tmp_path / "dev.key").exists()


def test_ssrf_guard(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("93.184.216.34", 0))])
    _assert_safe_stream_url("rtsp://camera.example.com/stream")
    _assert_safe_stream_url("https://example.com/stream.m3u8")

    for bad in ("file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/x",
                "http:///nohost", "notaurl"):
        with pytest.raises(HTTPException):
            _assert_safe_stream_url(bad)

    monkeypatch.setattr("socket.getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 0))])
    for internal in ("http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(HTTPException):
            _assert_safe_stream_url(internal)


def test_safe_upload_path_blocks_traversal():
    assert _safe_upload_path("") is None
    assert _safe_upload_path("../../etc/passwd") is None
    assert _safe_upload_path("/etc/passwd") is None
    assert _safe_upload_path("images/ok.jpg") is not None


class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_client_ip_ignores_untrusted_forwarding():
    # direct connection from the internet: the header is a lie
    assert client_ip(_Req("203.0.113.7", "1.2.3.4")) == "203.0.113.7"
    # behind our proxy: take the right-most entry that is not a proxy
    assert client_ip(_Req("172.18.0.5", "198.51.100.9")) == "198.51.100.9"
    # spoofed chain from a trusted proxy resolves to the real client, not the injected one
    assert client_ip(_Req("172.18.0.5", "9.9.9.9, 198.51.100.9")) == "198.51.100.9"


def test_rate_limiter_enforces_and_prunes():
    from app.main import _hits, _prune_rate_limits, limiter

    class _R(_Req):
        url = type("U", (), {"path": "/unit-test-limit"})()

    dep = limiter(2, "unit-test")
    req = _R("203.0.113.9")
    dep(req)
    dep(req)
    with pytest.raises(HTTPException) as exc:
        dep(req)
    assert exc.value.status_code == 429
    _hits[("unit-test", "203.0.113.9")].clear()
    _prune_rate_limits()
    assert ("unit-test", "203.0.113.9") not in _hits


# ------------------------------------------------------------------ zone metrics
def test_zone_metrics_on_a_known_square():
    """A 1x1 km square is 100 ha with a 1.41 km diagonal."""
    from shapely.geometry import Polygon

    from app import fire_model as fm
    lat, lon = 56.0, 92.9
    dlat = 1000 / 111320.0
    dlon = 1000 / (111320.0 * math.cos(math.radians(lat)))
    poly = Polygon([(lon, lat), (lon + dlon, lat), (lon + dlon, lat + dlat), (lon, lat + dlat)])
    m = fm.zone_metrics(poly, lat, lon)
    assert abs(m["area_ha"] - 100) < 5
    assert abs(m["diameter_m"] - 1414) < 30
    assert abs(m["perimeter_km"] - 4.0) < 0.1
    assert abs(m["head_distance_m"] - 1414) < 30


def test_zone_metrics_on_empty_polygon():
    from shapely.geometry import Polygon

    from app import fire_model as fm
    assert fm.zone_metrics(Polygon(), 56.0, 92.9)["diameter_m"] == 0.0
    assert fm.zone_metrics(None, 56.0, 92.9)["area_ha"] == 0.0


def test_hazard_polygon_reports_zone_size():
    poly, _cells, _n, diag = sv.hazard_polygon(56.0, 92.9, 10.0, 270.0, temp_c=30, humidity_pct=20)
    assert diag["diameter_m"] > 0
    assert diag["area_ha"] > 0
    # the diameter of the zone must not be smaller than its own bounding box width
    minx, _miny, maxx, _maxy = poly.bounds
    width_m = (maxx - minx) * 111320 * math.cos(math.radians(56.0))
    assert diag["diameter_m"] >= width_m * 0.9


def test_small_zone_is_smoothed():
    """A zone only a couple of cells across must not render as a jagged square."""
    poly, _cells, _n, diag = sv.hazard_polygon(56.0, 92.9, 1.0, 270.0, temp_c=10, humidity_pct=95,
                                               fuel="urban")
    # an ellipse has far more boundary vertices than a handful of grid squares
    assert len(poly.exterior.coords) > 12, len(poly.exterior.coords)
    assert diag["diameter_m"] > 0


# ------------------------------------------------------------------ overpass resilience
def test_failed_mirror_gets_cooled_down(monkeypatch):
    from app import services as svc
    calls = []

    class FakeResp:
        def raise_for_status(self):
            raise RuntimeError("boom")

    class FakeClient:
        async def post(self, url, **kw):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr(svc, "OVERPASS", ["http://a", "http://b"])
    monkeypatch.setattr(svc, "_MIRROR_STATE", {})
    monkeypatch.setattr(svc, "http_client", lambda: FakeClient())
    svc._CACHE.clear()

    import asyncio as _a
    with pytest.raises(RuntimeError):
        _a.run(svc.overpass("q", "cool-test-1"))
    # both mirrors were tried exactly once and both are now cooling down
    assert sorted(calls) == ["http://a", "http://b"]
    assert svc._all_mirrors_cooling() is True

    # the next call must fail immediately instead of waiting for timeouts again
    calls.clear()
    with pytest.raises(RuntimeError):
        _a.run(svc.overpass("q", "cool-test-2"))
    assert calls == [], "cooling mirrors must not be contacted again"


def test_overpass_negative_cache(monkeypatch):
    from app import services as svc
    calls = []

    class FakeResp:
        def raise_for_status(self):
            raise RuntimeError("boom")

    class FakeClient:
        async def post(self, url, **kw):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr(svc, "OVERPASS", ["http://a"])
    monkeypatch.setattr(svc, "_MIRROR_STATE", {})
    monkeypatch.setattr(svc, "http_client", lambda: FakeClient())
    svc._CACHE.clear()

    import asyncio as _a
    with pytest.raises(RuntimeError):
        _a.run(svc.overpass("q", "neg-test"))
    first = len(calls)
    with pytest.raises(RuntimeError):
        _a.run(svc.overpass("q", "neg-test"))
    assert len(calls) == first, "a recent failure must be cached, not retried"


def test_gather_with_deadline_returns_partial_results():
    import asyncio as _a

    from app.main import _gather_with_deadline

    async def fast():
        return "ok"

    async def slow():
        await _a.sleep(5)
        return "never"

    out = _a.run(_gather_with_deadline(0.2, fast=fast(), slow=slow()))
    assert out["fast"] == "ok"
    assert out["slow"] is None


# ------------------------------------------------------------------ image weight
def test_images_are_downscaled_and_compressed(tmp_path):
    """A 12 MP photo must not be stored at full weight."""
    import cv2
    import numpy as np

    from app import config
    from app import services as svc
    big = np.zeros((3000, 4000, 3), dtype="uint8")   # 12 MP
    big[:, :, 1] = 180
    big[::7, ::7] = 255                              # some detail so JPEG is not trivial
    out = tmp_path / "big.jpg"
    assert svc.save_image(out, big) is True
    saved = cv2.imread(str(out))
    assert saved.shape[1] == int(config._int("IMAGE_MAX_WIDTH", 1600))
    assert out.stat().st_size < 2_000_000, out.stat().st_size

    thumb = tmp_path / "thumb.jpg"
    assert svc.make_thumbnail(out, thumb) is True
    assert cv2.imread(str(thumb)).shape[1] == int(config._int("THUMB_WIDTH", 320))
    assert thumb.stat().st_size < out.stat().st_size
