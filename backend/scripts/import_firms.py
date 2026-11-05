#!/usr/bin/env python
"""Import satellite active-fire detections from NASA FIRMS.

FIRMS publishes near-real-time hotspots from MODIS and VIIRS as CSV, free of
charge; only a map key is required (https://firms.modaps.eosdis.nasa.gov/api/).

    # one-off import of the last 24 h over Siberia
    python -m scripts.import_firms --bbox 60,50,120,75 --days 1

    # or set FIRMS_MAP_KEY in .env and let the server poll automatically
    #   FIRMS_MAP_KEY=...   FIRMS_POLL_MINUTES=30

Each row becomes an incident with source=SATELLITE. Detections are deduplicated
against existing satellite incidents from the same acquisition (same day, within
FIRMS_DEDUPE_KM), so repeated polls do not pile up duplicates.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_conf import setup_logging  # noqa: E402
from app.models import GpsSource, Incident, SessionLocal, Source, Status, get_db, init_db  # noqa: E402,F401
from app.services import haversine_km  # noqa: E402

FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}/{date}"
DEFAULT_BBOX = "19,41,180,82"        # Russia
DEFAULT_SOURCE = "VIIRS_SNPP_NRT"

#: FIRMS confidence -> detector-style score, so the usual escalation tiers apply.
TEXT_CONFIDENCE = {"l": 0.5, "n": 0.7, "h": 0.9}


def parse_confidence(raw: str) -> float:
    """FIRMS reports either 'l/n/h' (MODIS) or 0-100 (VIIRS)."""
    value = (raw or "").strip().lower()
    if value in TEXT_CONFIDENCE:
        return TEXT_CONFIDENCE[value]
    try:
        return max(0.0, min(float(value) / 100.0, 1.0))
    except ValueError:
        return 0.0


def fetch_csv(map_key: str, source: str, bbox: str, days: int, timeout: float = 60.0) -> list[dict]:
    date = (datetime.now(timezone.utc) - timedelta(days=max(0, days - 1))).strftime("%Y-%m-%d")
    url = FIRMS_URL.format(key=map_key, source=source, bbox=bbox, days=days, date=date)
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
        text = r.text
    if text.lstrip().lower().startswith("invalid"):
        raise RuntimeError(f"FIRMS отклонил запрос: {text[:120]}")
    return list(csv.DictReader(io.StringIO(text)))


def import_rows(rows: list[dict], min_confidence: float, dedupe_km: float,
                enrich: bool = True) -> tuple[int, int]:
    """Insert new hotspots. Returns (created, skipped_as_duplicate)."""
    created = skipped = 0
    with SessionLocal() as db:
        recent = db.query(Incident).filter(Incident.source == Source.SATELLITE).all()
        for row in rows:
            try:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            conf = parse_confidence(row.get("confidence", ""))
            if conf < min_confidence:
                continue
            if any(haversine_km(lat, lon, r.lat, r.lon) <= dedupe_km for r in recent if r.lat):
                skipped += 1
                continue
            acq = f"{row.get('acq_date', '')} {row.get('acq_time', '')}".strip()
            sat = row.get("satellite") or row.get("instrument") or "FIRMS"
            i = Incident(source=Source.SATELLITE, status=Status.PENDING_VERIFICATION,
                         confidence=conf, lat=lat, lon=lon, gps_source=GpsSource.SATELLITE,
                         detections=[{"cls": "satellite_hotspot", "conf": conf,
                                      "satellite": sat, "acquired": acq,
                                      "frp": row.get("frp"), "brightness": row.get("bright_ti4")
                                      or row.get("brightness")}],
                         region_name=f"Спутник: {sat} ({acq})")
            db.add(i)
            recent.append(i)
            created += 1
        db.commit()
    return created, skipped


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(prog="import_firms", description="Импорт спутниковых очагов NASA FIRMS")
    p.add_argument("--map-key", default=os.getenv("FIRMS_MAP_KEY", ""))
    p.add_argument("--source", default=os.getenv("FIRMS_SOURCE", DEFAULT_SOURCE))
    p.add_argument("--bbox", default=os.getenv("FIRMS_BBOX", DEFAULT_BBOX),
                   help="west,south,east,north")
    p.add_argument("--days", type=int, default=1)
    p.add_argument("--min-confidence", type=float, default=float(os.getenv("FIRMS_MIN_CONFIDENCE", 0)))
    p.add_argument("--dedupe-km", type=float, default=float(os.getenv("FIRMS_DEDUPE_KM", 1.0)))
    p.add_argument("--no-enrich", action="store_true", help="не считать зону/погоду сразу")
    args = p.parse_args(argv)

    if not args.map_key:
        raise SystemExit("Нужен ключ FIRMS: --map-key или FIRMS_MAP_KEY в .env "
                         "(получить: https://firms.modaps.eosdis.nasa.gov/api/map_key/)")
    init_db()
    rows = fetch_csv(args.map_key, args.source, args.bbox, args.days)
    print(f"FIRMS: получено {len(rows)} детекций")
    created, skipped = import_rows(rows, args.min_confidence, args.dedupe_km)
    print(f"добавлено {created}, пропущено как дубликаты {skipped}")
    if created and not args.no_enrich:
        print("Подсказка: откройте инциденты в интерфейсе или вызовите "
              "POST /incidents/{id}/refresh, чтобы посчитать зону и погоду.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
