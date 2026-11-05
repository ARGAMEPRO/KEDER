"""Logging configuration: structured, level-controlled, no coordinate leakage by default."""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = (os.getenv("LOG_LEVEL") or ("INFO" if os.getenv("APP_ENV", "dev").lower() not in ("prod", "production")
                                        else "WARNING")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))

    # Third-party chatter we never want at INFO.
    for noisy in ("httpx", "httpcore", "urllib3", "alembic.runtime.migration", "alembic.runtime.plugins",
                  "python_multipart", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger("kedr").info("logging configured at %s", level)
