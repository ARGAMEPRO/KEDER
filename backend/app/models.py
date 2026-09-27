"""SQLAlchemy models, DB session and the three-tier escalation rule for КЕДР."""
import enum
import os
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./kedr.db")
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

TIER_PENDING = float(os.getenv("TIER_PENDING", 0.40))
TIER_CRITICAL = float(os.getenv("TIER_CRITICAL", 0.80))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    CITIZEN = "citizen"
    FORESTER = "forester"
    MCHS = "mchs"


class Status(str, enum.Enum):
    CLEAR = "CLEAR"                                # Stage 1: conf < 40%
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # Stage 2: 40% <= conf < 80%
    CRITICAL_ALERT = "CRITICAL_ALERT"              # Stage 3: conf >= 80%
    FALSE_ALARM = "FALSE_ALARM"                    # forester rejected Stage 2
    CONFIRMED = "CONFIRMED"                        # forester confirmed Stage 2


class Source(str, enum.Enum):
    PHOTO = "PHOTO"
    VIDEO = "VIDEO"
    RTSP = "RTSP"
    MANUAL = "MANUAL"


class GpsSource(str, enum.Enum):
    EXIF = "EXIF"
    DEVICE = "DEVICE"
    MANUAL = "MANUAL"
    SETTLEMENT = "SETTLEMENT"
    NONE = "NONE"


def classify(conf: float) -> Status:
    """conf is YOLO's native 0..1 score."""
    if conf >= TIER_CRITICAL:
        return Status.CRITICAL_ALERT
    if conf >= TIER_PENDING:
        return Status.PENDING_VERIFICATION
    return Status.CLEAR


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.CITIZEN)


class Settlement(Base):
    __tablename__ = "settlements"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    name_en: Mapped[str] = mapped_column(String(255), default="")
    region: Mapped[str] = mapped_column(String(255), default="")
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    population: Mapped[int] = mapped_column(Integer, default=0)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[Source] = mapped_column(Enum(Source))
    status: Mapped[Status] = mapped_column(Enum(Status), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Position: decimal degrees, stored at full float precision (display as DD.DDDDDD)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_source: Mapped[GpsSource] = mapped_column(Enum(GpsSource), default=GpsSource.NONE)
    region_name: Mapped[str] = mapped_column(String(512), default="")

    image_path: Mapped[str] = mapped_column(String(512), default="")      # under /uploads
    annotated_path: Mapped[str] = mapped_column(String(512), default="")
    detections: Mapped[list] = mapped_column(JSON, default=list)          # [{cls, conf, xyxy}]

    # Weather snapshot (Open-Meteo, with fallback defaults flagged below)
    wind_speed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_dir_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    weather_fallback: Mapped[int] = mapped_column(Integer, default=0)

    # Hazard polygon (GeoJSON) and settlements it intersects
    hazard_geojson: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evacuation: Mapped[list] = mapped_column(JSON, default=list)          # [{name, population, distance_km, bearing}]

    reporter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reporter: Mapped[User | None] = relationship(foreign_keys=[reporter_id])
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id])


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
