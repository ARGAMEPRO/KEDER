"""Programmatic Alembic integration.

Using Alembic as the single source of truth keeps SQLite (zero-config) and
PostgreSQL (production) schemas identical.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("kedr.migrations")

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def alembic_available() -> bool:
    try:
        import alembic  # noqa: F401,PLC0415
        return ALEMBIC_INI.exists()
    except Exception:
        return False


def upgrade_to_head() -> bool:
    """Run `alembic upgrade head`. Returns False when Alembic is not configured."""
    if not alembic_available():
        return False
    from alembic import command
    from alembic.config import Config

    from .config import DATABASE_URL

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    command.upgrade(cfg, "head")
    log.info("database schema is at head")
    return True
