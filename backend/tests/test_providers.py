"""Geo providers: response parsing and the fallback chain that keeps the UI alive."""
from __future__ import annotations

import asyncio

import pytest

from app import services as sv

# conftest stubs the public helpers for API tests; keep the real ones for the chain tests.
_real_weather = sv.weather
_real_infrastructure = sv.infrastructure_near

# Real `ymapsbm1://geo?data=...` payload returned by Yandex suggest for "Москва".
MOSCOW_URI = ("ymapsbm1://geo?data=Cgg1MzE2NjM5MxIa0KDQvtGB0YHQuNGPLCDQnNC-0YHQutCy0LAi"
              "Cg2GeBZCFQEGX0I")


def test_ymaps_point_decodes_suggest_payload():
    lat, lon = sv.ymaps_point(MOSCOW_URI)
    assert lat == pytest.approx(55.7559, abs=1e-3)
    assert lon == pytest.approx(37.6177, abs=1e-3)
    assert sv.ymaps_point("") is None
    assert sv.ymaps_point("ymapsbm1://geo?data=@@not-base64@@") is None
    assert sv.ymaps_point("no-data-here") is None


def test_geoapify_kind_prefers_osm_tags():
    assert sv._geoapify_kind({"datasource": {"raw": {"amenity": "hospital"}}})[0] == "hospital"
    assert sv._geoapify_kind({"datasource": {"raw": {"power": "plant", "plant:source": "nuclear"}}})[0] == "nuclear"
    assert sv._geoapify_kind({"categories": ["service.fire_station"], "datasource": {}})[0] == "fire_station"
    assert sv._geoapify_kind({"categories": ["catering.cafe"]})[0] == "other"


def test_weather_chain_falls_back_to_the_next_provider(monkeypatch):
    async def blocked(lat, lon):
        raise RuntimeError("blocked in RU")

    async def working(lat, lon):
        return {"wind_speed_ms": 3.0, "wind_dir_deg": 90, "temp_c": 10.0, "humidity_pct": 40}

    monkeypatch.setattr(sv, "_wx_open_meteo", blocked)
    monkeypatch.setattr(sv, "_wx_metno", blocked)
    monkeypatch.setattr(sv, "_wx_openweathermap", blocked)
    monkeypatch.setattr(sv, "_wx_wttr", working)
    sv._CACHE.clear()

    data, err = asyncio.run(_real_weather(1.0, 2.0))
    assert err == ""
    assert data["wind_speed_ms"] == 3.0
    assert data["humidity_pct"] == 40


def _hospital_row():
    return {"kind": "hospital", "criticality": 4, "name": "ГКБ №1", "lat": 55.7601, "lon": 37.6101,
            "beds": 300, "capacity": None, "emergency": True, "phone": None,
            "voltage": None, "plant_source": None, "operator": None}


def test_infrastructure_uses_fallback_and_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GEOAPIFY_KEY", "test-key")  # enables the Geoapify branch
    monkeypatch.delenv("TWOGIS_KEY", raising=False)
    monkeypatch.setenv("INFRA_OSM_WAIT", "0.2")
    monkeypatch.setattr(sv, "_PCACHE_PATH", tmp_path / "geo_cache.sqlite")
    monkeypatch.setattr(sv, "_pcache_ready", False)
    monkeypatch.setattr(sv, "_pcache_broken", False)
    sv._CACHE.clear()

    async def no_overpass(*a, **k):
        raise RuntimeError("overpass blocked")

    async def no_rows(*a, **k):
        return []

    async def rows(*a, **k):
        return [_hospital_row()]

    monkeypatch.setattr(sv, "overpass", no_overpass)
    monkeypatch.setattr(sv, "geoapify_near", no_rows)
    monkeypatch.setattr(sv, "twogis_near", no_rows)
    assert asyncio.run(_real_infrastructure(55.7558, 37.6173, 12000, 12000)) == ([], "none")

    monkeypatch.setattr(sv, "geoapify_near", rows)
    got, source = asyncio.run(_real_infrastructure(55.7558, 37.6173, 12000, 12000))
    assert source == "geoapify"
    assert got and got[0]["kind"] == "hospital"
    assert got[0]["distance_km"] > 0

    monkeypatch.setattr(sv, "geoapify_near", no_rows)
    sv._CACHE.clear()  # drop the in-process copy, leave only the disk cache
    cached, source = asyncio.run(_real_infrastructure(55.7558, 37.6173, 12000, 12000))
    assert source == "cache"
    assert len(cached) == len(got)


def test_infrastructure_prefers_overpass_when_it_answers(monkeypatch, tmp_path):
    monkeypatch.setenv("INFRA_OSM_WAIT", "2")
    monkeypatch.setattr(sv, "_PCACHE_PATH", tmp_path / "geo_cache.sqlite")
    monkeypatch.setattr(sv, "_pcache_ready", False)
    monkeypatch.setattr(sv, "_pcache_broken", False)
    sv._CACHE.clear()

    async def osm(*a, **k):
        return {"elements": [{"type": "node", "id": 1, "lat": 55.7601, "lon": 37.6101,
                              "tags": {"amenity": "fire_station", "name": "ПЧ-13"}}]}

    async def must_not_run(*a, **k):
        raise AssertionError("fallback provider should not be called")

    monkeypatch.setattr(sv, "overpass", osm)
    monkeypatch.setattr(sv, "geoapify_near", must_not_run)
    rows, source = asyncio.run(_real_infrastructure(55.7558, 37.6173, 12000, 12000))
    assert source == "osm"
    assert rows[0]["kind"] == "fire_station"


def test_overpass_retries_a_transient_502(monkeypatch):
    """A public instance answering "502 at capacity" is retried before cooldown."""
    from app import services as svc

    calls = []

    class Resp:
        def __init__(self, ok):
            self.ok = ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("502 at capacity")

        def json(self):
            return {"elements": []}

    class Client:
        async def post(self, url, **kw):
            calls.append(url)
            return Resp(len(calls) > 1)

    monkeypatch.setattr(svc, "OVERPASS", ["http://retry"])
    monkeypatch.setattr(svc, "_MIRROR_STATE", {})
    monkeypatch.setattr(svc, "http_client", lambda: Client())
    monkeypatch.setenv("OVERPASS_ATTEMPTS", "2")
    svc._CACHE.clear()

    assert asyncio.run(svc.overpass("q", "retry-test-1")) == {"elements": []}
    assert len(calls) == 2
