"""Test configuration: isolated database, uploads dir and stubbed external services."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import numpy as np

TMP = Path(tempfile.mkdtemp(prefix="kedr-tests-"))
os.environ["APP_ENV"] = "dev"
os.environ["DATABASE_URL"] = f"sqlite:///{TMP / 'test.db'}"
os.environ["UPLOAD_DIR"] = str(TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-long-enough-for-tests-0123456789"
os.environ["SEED_ON_STARTUP"] = "false"
os.environ["YOLO_WEIGHTS"] = ""
os.environ["YOLO_WEIGHTS_URL"] = ""
os.environ["SERVE_UPLOADS_PUBLIC"] = "false"
os.environ["ENABLE_FIRE_BARRIERS"] = "false"
# generous limits so functional tests are not throttled (rate limiting is unit-tested)
for var in ("RATE_LIMIT_LOGIN", "RATE_LIMIT_REGISTER", "RATE_LIMIT_PER_MIN"):
    os.environ[var] = "10000"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import services as sv  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Incident, Role, SessionLocal, Settlement, User, engine  # noqa: E402
from app.security import hash_password  # noqa: E402

FORESTER = {"email": "forester@test.ru", "password": "ForestPass1"}
MCHS = {"email": "mchs@test.ru", "password": "MchsPass123"}
CITIZEN = {"email": "citizen@test.ru", "password": "Citizen123"}


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    yield
    shutil.rmtree(TMP, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_db():
    with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())
        db.commit()
    yield


@pytest.fixture(autouse=True)
def _stub_external(monkeypatch):
    """Keep every test hermetic: no Overpass, Nominatim, Open-Meteo or YOLO."""
    async def fake_weather(lat, lon):
        return ({"wind_speed_ms": 6.0, "wind_dir_deg": 270.0, "temp_c": 24.0, "humidity_pct": 35.0}, "")

    async def fake_geocode(lat, lon):
        return ("Тестовый район, Россия", "ru")

    async def fake_nearby(lat, lon, radius_m=20000):
        return {"places": [{"name": "Тестовый город", "lat": lat + 0.002, "lon": lon + 0.002,
                            "type": "town", "population": 12000}],
                "landuse": [{"kind": "forest", "lat": lat, "lon": lon}], "source": "osm"}

    async def fake_infra(lat, lon, radius_m=15000, hazard_radius_m=50000):
        return ([{"kind": "hospital", "criticality": 4, "name": "Больница №1", "lat": lat + 0.01,
                  "lon": lon + 0.01, "distance_km": 1.2, "beds": 100, "capacity": None, "emergency": True,
                  "phone": None, "voltage": None, "plant_source": None, "operator": None}], "osm")

    async def fake_barriers(lat, lon, radius_m, fire_polygon=None):
        return []

    monkeypatch.setattr(sv, "weather", fake_weather)
    monkeypatch.setattr(sv, "reverse_geocode", fake_geocode)
    monkeypatch.setattr(sv, "nearby", fake_nearby)
    monkeypatch.setattr(sv, "infrastructure_near", fake_infra)
    monkeypatch.setattr(sv, "check_fire_barriers", fake_barriers)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user():
    def _make(email: str, password: str, role: Role) -> User:
        with SessionLocal() as db:
            u = User(email=email, full_name=email.split("@")[0], role=role,
                     hashed_password=hash_password(password))
            db.add(u)
            db.commit()
            db.refresh(u)
            db.expunge(u)
            return u

    return _make


@pytest.fixture
def forester(make_user):
    make_user(FORESTER["email"], FORESTER["password"], Role.FORESTER)
    return FORESTER


@pytest.fixture
def mchs(make_user):
    make_user(MCHS["email"], MCHS["password"], Role.MCHS)
    return MCHS


@pytest.fixture
def citizen(make_user):
    make_user(CITIZEN["email"], CITIZEN["password"], Role.CITIZEN)
    return CITIZEN


def auth(client, creds) -> dict:
    r = client.post("/api/v1/auth/login", json=creds)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def forester_headers(client, forester):
    return auth(client, forester)


@pytest.fixture
def mchs_headers(client, mchs):
    return auth(client, mchs)


@pytest.fixture
def citizen_headers(client, citizen):
    return auth(client, citizen)


def fake_detection(conf: float = 0.92, cls: str = "fire"):
    """Synthetic (conf, dets, raw, ann) tuple replacing the YOLO call."""
    dets = [{"cls": cls, "conf": conf, "xyxy": [1.0, 2.0, 30.0, 40.0]}]
    raw = np.zeros((64, 64, 3), dtype=np.uint8)
    ann = np.zeros((64, 64, 3), dtype=np.uint8)
    return conf, dets, raw, ann


@pytest.fixture
def stub_analyze(monkeypatch):
    def _stub(result=None):
        monkeypatch.setattr(sv, "analyze", lambda *a, **k: result if result is not None else fake_detection())
    return _stub


@pytest.fixture
def seed_settlement():
    def _seed(name="Красноярск", lat=56.0153, lon=92.8932, population=1190000):
        with SessionLocal() as db:
            s = Settlement(name=name, name_en="", region="Красноярский край", lat=lat, lon=lon,
                           population=population)
            db.add(s)
            db.commit()

    return _seed


def incident_count() -> int:
    with SessionLocal() as db:
        return db.query(Incident).count()
