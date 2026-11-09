"""Central configuration and startup validation for КЕДР.

Everything security- or deployment-critical is resolved here, in one place, so
that misconfiguration fails loudly at boot instead of silently in production.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("kedr.config")

# Secrets that ship in the repository / documentation and must never sign real tokens.
WEAK_SECRETS = {
    "",
    "dev-secret-change-me",
    "dev",
    "change-me",
    "change-me-in-production",
    "changeme",
    "secret",
    "kedr",
    "test",
    "password",
}
MIN_SECRET_LEN = 32

PROD_ENVS = {"prod", "production"}


def env_name() -> str:
    return (os.getenv("APP_ENV") or "dev").strip().lower()


def is_production() -> bool:
    return env_name() in PROD_ENVS


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # fail fast: a typo in .env must not become a silent default
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- secret key
def _dev_secret_file() -> Path:
    explicit = (os.getenv("DEV_SECRET_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parent.parent / "data" / "dev_secret.key"


def _persistent_dev_secret() -> str:
    """Generate a dev key once and keep it, so tokens survive a restart.

    The file is git-ignored and never used in production.
    """
    path = _dev_secret_file()
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if len(existing) >= MIN_SECRET_LEN:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        path.write_text(generated, encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        log.warning("Generated a development SECRET_KEY at %s. "
                    "It is git-ignored; production never uses it.", path)
        return generated
    except OSError as exc:
        log.warning("Could not persist a development key (%s); using a random one.", exc)
        return secrets.token_urlsafe(48)


def resolve_secret_key() -> str:
    """Return a usable signing key, or refuse to start in production.

    dev  : a missing/placeholder key becomes a stable, locally stored random key
           (safe by default, and tokens survive restarts).
    prod : a missing, placeholder or too-short key is a hard startup error.
    """
    raw = (os.getenv("SECRET_KEY") or "").strip()
    weak = raw.lower() in WEAK_SECRETS or len(raw) < MIN_SECRET_LEN

    if is_production():
        if not raw:
            raise RuntimeError(
                "SECRET_KEY is not set. Generate one with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if weak:
            reason = ("is a well-known placeholder" if raw.lower() in WEAK_SECRETS
                      else f"is shorter than {MIN_SECRET_LEN} characters")
            raise RuntimeError(
                f"SECRET_KEY {reason}. Refusing to start with APP_ENV={env_name()}. "
                "Generate a strong key with:\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return raw

    if weak:
        return _persistent_dev_secret()
    return raw


SECRET = resolve_secret_key()
ALGORITHM = "HS256"
JWT_ISSUER = os.getenv("JWT_ISSUER", "kedr")
JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "kedr-api")


# ------------------------------------------------------------------ paths
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _resolve_path(raw: str) -> Path:
    """Relative paths are resolved against backend/, not the current directory.

    This keeps `weights/` and `uploads/` correct whether the app is started from
    backend/, from the repository root, or by an arbitrary process manager.
    """
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (BACKEND_DIR / p)


# ------------------------------------------------------------------ database
def database_url() -> str:
    """Resolve the database URL from DATABASE_URL, or build one from POSTGRES_*.

    Precedence:
      1. DATABASE_URL (explicit, wins);
      2. POSTGRES_HOST set -> build a PostgreSQL URL (this is what compose does);
      3. otherwise a local SQLite file.
    """
    from urllib.parse import quote_plus

    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        host = (os.getenv("POSTGRES_HOST") or "").strip()
        if host:
            user = os.getenv("POSTGRES_USER") or "kedr"
            password = os.getenv("POSTGRES_PASSWORD") or ""
            name = os.getenv("POSTGRES_DB") or "kedr"
            port = os.getenv("POSTGRES_PORT") or "5432"
            if not password:
                raise RuntimeError(
                    "POSTGRES_PASSWORD is not set. It is required when POSTGRES_HOST is used. "
                    "Generate one and put it in .env."
                )
            url = (f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
                   f"@{host}:{port}/{name}")
        else:
            url = "sqlite:///./kedr.db"

    # Heroku/Render style URLs must be rewritten for SQLAlchemy + psycopg3.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    # A relative SQLite file must not depend on the current directory.
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///"):]
        if rel and rel != ":memory:" and not Path(rel).is_absolute():
            url = "sqlite:///" + str((BACKEND_DIR / rel).resolve())
    return url


DATABASE_URL = database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")


# ------------------------------------------------------------------ uploads
def upload_dir() -> Path:
    return _resolve_path(os.getenv("UPLOAD_DIR") or "uploads")


UPLOADS = upload_dir()
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 100)


# ------------------------------------------------------------------ detection
def weights_path() -> str:
    """Locate the model weights.

    Order: the configured/default file, then any single `.pt` dropped into the
    weights directory. Operators routinely name their checkpoint `best.pt`, and
    "no weights found" for a file that is plainly there is a bad failure mode.
    """
    weights_dir = _resolve_path(os.getenv("WEIGHTS_DIR") or "weights")
    configured = (os.getenv("YOLO_WEIGHTS") or "").strip() or str(weights_dir / "best.pt")
    path = _resolve_path(configured)
    if path.exists():
        return str(path)
    if weights_dir.is_dir():
        found = sorted(p for p in weights_dir.glob("*.pt") if p.is_file())
        if found:
            return str(found[0])
    return str(path)


def weights_sha256() -> str:
    return (os.getenv("YOLO_WEIGHTS_SHA256") or "").strip().lower()


def detect_device() -> str:
    """`auto` picks CUDA when the vision stack is actually available, else CPU."""
    requested = (os.getenv("YOLO_DEVICE") or "auto").strip().lower()
    if requested and requested != "auto":
        return requested
    try:
        import torch  # noqa: PLC0415  (optional heavy dependency)

        if torch.cuda.is_available():
            return "cuda:0"
    except Exception:  # torch missing is fine — CPU is the safe default
        pass
    return "cpu"


# ------------------------------------------------------------------ auth
ACCESS_TOKEN_EXPIRE_MINUTES = _int("ACCESS_TOKEN_EXPIRE_MINUTES", 720)
MIN_PASSWORD_LEN = _int("MIN_PASSWORD_LEN", 8)
MAX_PASSWORD_LEN = 128


# ------------------------------------------------------------------ seeding
def seed_enabled() -> bool:
    """Demo data is opt-in, and production always requires the explicit override."""
    explicit = (os.getenv("SEED_ON_STARTUP") or "").strip().lower()
    if is_production():
        # In production seeding requires a second, deliberate confirmation flag.
        return explicit in ("1", "true", "yes", "on") and _bool("SEED_ALLOW_IN_PRODUCTION", False)
    if explicit:
        return explicit in ("1", "true", "yes", "on")
    return True


# ------------------------------------------------------------------ limits
def rate_limit(name: str, default: int) -> int:
    return _int(name, default)


OVERPASS_TIMEOUT = _float("OVERPASS_TIMEOUT", 15)
