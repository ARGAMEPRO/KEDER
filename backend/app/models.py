"""SQLAlchemy models, DB session and the three-tier escalation rule for КЕДР."""
from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import DATABASE_URL, IS_SQLITE, _float  # noqa: F401  (re-exported for convenience)

log = logging.getLogger("kedr.db")

# ---------------------------------------------------------------- escalation
# Fire and smoke are escalated on different scales: smoke is an early warning,
# not a confirmed open flame, so it needs more evidence to become critical.
TIER_PENDING = _float("TIER_PENDING", 0.40)
TIER_CRITICAL = _float("TIER_CRITICAL", 0.80)
TIER_PENDING_SMOKE = _float("TIER_PENDING_SMOKE", 0.60)
TIER_CRITICAL_SMOKE = _float("TIER_CRITICAL_SMOKE", 0.90)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    CITIZEN = "citizen"
    FORESTER = "forester"
    MCHS = "mchs"


class Status(str, enum.Enum):
    CLEAR = "CLEAR"                                # Stage 1: conf < TIER_PENDING
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # Stage 2: forester review
    CRITICAL_ALERT = "CRITICAL_ALERT"              # Stage 3: dispatch MCHS now
    FALSE_ALARM = "FALSE_ALARM"                    # forester rejected Stage 2
    CONFIRMED = "CONFIRMED"                        # forester/MCHS confirmed


class Source(str, enum.Enum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    RTSP = "RTSP"
    MANUAL = "MANUAL"
    SATELLITE = "SATELLITE"   # imported satellite hotspot (e.g. NASA FIRMS)


class GpsSource(str, enum.Enum):
    EXIF = "EXIF"
    SATELLITE = "SATELLITE"
    DEVICE = "DEVICE"
    MANUAL = "MANUAL"
    SETTLEMENT = "SETTLEMENT"
    NONE = "NONE"


# Class names the YOLO model is expected to emit. Anything containing "smoke"
# is treated as an early warning, everything else as fire.
SMOKE_HINTS = ("smoke", "дым")


def _is_smoke(kind: str | None) -> bool:
    if not kind:
        return False
    low = kind.lower()
    return any(h in low for h in SMOKE_HINTS)


def classify(conf: float, kind: str | None = None) -> Status:
    """Map a detection score to a status, using the smoke or fire thresholds."""
    smoke = _is_smoke(kind)
    pending = TIER_PENDING_SMOKE if smoke else TIER_PENDING
    critical = TIER_CRITICAL_SMOKE if smoke else TIER_CRITICAL
    if conf >= critical:
        return Status.CRITICAL_ALERT
    if conf >= pending:
        return Status.PENDING_VERIFICATION
    return Status.CLEAR


def classify_detections(conf: float, detections: list | None) -> Status:
    """Classify using the strongest *class-aware* verdict across detections.

    A 0.95 smoke hit must not be treated the same as a 0.95 open-flame hit.
    """
    statuses = [classify(float(d.get("conf") or 0.0), d.get("cls")) for d in (detections or []) if isinstance(d, dict)]
    if not statuses:
        return classify(conf)
    order = [Status.CLEAR, Status.PENDING_VERIFICATION, Status.CRITICAL_ALERT]
    return max(statuses, key=order.index)


# ---------------------------------------------------------------- engine
_connect_args = {"check_same_thread": False, "timeout": 30} if IS_SQLITE else {}
_engine_kwargs: dict = {"connect_args": _connect_args, "pool_pre_ping": True, "future": True}
if not IS_SQLITE:
    _engine_kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")       # readers don't block the writer
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")        # enforce ON DELETE SET NULL
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ---------------------------------------------------------------- tables
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=32), default=Role.CITIZEN)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Bumped on password change / forced logout so old JWTs stop working.
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    name_en: Mapped[str] = mapped_column(String(255), default="")
    region: Mapped[str] = mapped_column(String(255), default="")
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    population: Mapped[int] = mapped_column(Integer, default=0, index=True)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[Source] = mapped_column(Enum(Source, native_enum=False, length=32))
    status: Mapped[Status] = mapped_column(Enum(Status, native_enum=False, length=32), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Position: decimal degrees, full float precision (displayed as DD.DDDDDD)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_source: Mapped[GpsSource] = mapped_column(Enum(GpsSource, native_enum=False, length=32), default=GpsSource.NONE)
    region_name: Mapped[str] = mapped_column(String(512), default="")

    image_path: Mapped[str] = mapped_column(String(512), default="")
    annotated_path: Mapped[str] = mapped_column(String(512), default="")
    thumb_path: Mapped[str] = mapped_column(String(512), default="")
    detections: Mapped[list] = mapped_column(JSON, default=list)

    # Weather snapshot (Open-Meteo); weather_fallback=1 means "unavailable".
    wind_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_dir_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather_fallback: Mapped[int] = mapped_column(Integer, default=0)

    # Hazard polygon (compact GeoJSON) and the settlements it threatens
    hazard_geojson: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evacuation: Mapped[list] = mapped_column(JSON, default=list)

    reporter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reporter: Mapped[User | None] = relationship(foreign_keys=[reporter_id])
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id])


# ---------------------------------------------------------------- session
def _stamp_head() -> None:
    try:
        from alembic import command
        from alembic.config import Config

        from .config import DATABASE_URL
        from .migrations import ALEMBIC_INI, BACKEND_DIR

        cfg = Config(str(ALEMBIC_INI))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
        command.stamp(cfg, "head")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("could not stamp alembic head: %s", exc)


def _schema_guard() -> None:
    """Add columns present in the models but missing from an existing table.

    Keeps a database created by an older release usable instead of failing later
    with an opaque "no such column" error.
    """
    from sqlalchemy import inspect
    from sqlalchemy import text as _text

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'
                default = getattr(col.default, "arg", None) if col.default is not None else None
                if default is not None and not callable(default):
                    if isinstance(default, bool):
                        ddl += f" DEFAULT {1 if default else 0}"
                    elif isinstance(default, (int, float)):
                        ddl += f" DEFAULT {default}"
                    else:
                        ddl += f" DEFAULT '{default}'"
                elif not col.nullable:
                    ddl += " DEFAULT " + ("0" if str(col.type).startswith(("INTEGER", "FLOAT", "BOOLEAN")) else "''")
                conn.execute(_text(ddl))
                log.warning("schema guard: added %s.%s", table.name, col.name)


def init_db() -> None:
    """Create or migrate the schema.

    Alembic is the source of truth when available (and required for PostgreSQL);
    `create_all` plus the guard keeps a bare SQLite checkout working with zero setup.
    """
    from sqlalchemy import inspect

    tables = set(inspect(engine).get_table_names())
    legacy = "users" in tables and "alembic_version" not in tables

    if legacy:
        log.info("legacy database detected: applying create_all + schema guard")
        Base.metadata.create_all(engine)
        _schema_guard()
        _stamp_head()
        return

    migrated = False
    try:
        from .migrations import upgrade_to_head  # noqa: PLC0415

        migrated = upgrade_to_head()
    except Exception as exc:
        log.warning("alembic migration failed (%s); falling back to create_all()", exc)
    if not migrated:
        Base.metadata.create_all(engine)
    _schema_guard()


def db_healthy() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
