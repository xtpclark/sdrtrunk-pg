#!/usr/bin/env python3
"""
Load city address data into the address_db table.

Reads source configuration from the active city config
(data/cities/{slug}/config.yaml) and downloads address points
from the configured open data source (Socrata CSV or ArcGIS JSON).

Usage:
    python scripts/load_address_db.py              # loads primary city
    python scripts/load_address_db.py --all        # loads primary + additional_cities
    python scripts/load_address_db.py --city norfolk-va
    python scripts/load_address_db.py --clear      # wipe address_db first

Supported sources:
    socrata   — Socrata API (data.norfolk.gov, data.cityofchicago.org, etc.)
    arcgis    — ArcGIS Feature Service query endpoint
    csv       — plain CSV with lat/lon columns (local file or URL)
"""

import argparse
import csv
import io
import logging
import sys
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("load_address_db")

from app.db import db


def _ensure_schema():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS address_db (
                id            serial PRIMARY KEY,
                full_address  text NOT NULL,
                house_number  text,
                street_name   text,
                street_type   text,
                predirectional text,
                full_street   text,
                lat           double precision NOT NULL,
                lon           double precision NOT NULL,
                city          text NOT NULL DEFAULT '',
                source        text NOT NULL DEFAULT ''
            );
            CREATE EXTENSION IF NOT EXISTS pg_trgm;
            CREATE INDEX IF NOT EXISTS address_db_trgm_idx
                ON address_db USING GIN (full_address gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS address_db_street_trgm_idx
                ON address_db USING GIN (full_street gin_trgm_ops);
            CREATE INDEX IF NOT EXISTS address_db_house_street_idx
                ON address_db (house_number, street_name);
        """)
    log.info("Schema ready.")


def _clear_city(city_value: str):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM address_db WHERE city = %s", (city_value,))
        log.info("Cleared existing records for city=%r", city_value)


def _load_records(rows: list[dict], field_map: dict, city_value: str, source_name: str) -> int:
    """Insert rows into address_db, returning count inserted."""
    fa_field  = field_map.get("full_address", "full_address")
    hn_field  = field_map.get("house_number", "house_number")
    sn_field  = field_map.get("street_name",  "street_name")
    st_field  = field_map.get("street_type",  "street_type")
    pd_field  = field_map.get("predirectional", "predirectional")
    fs_field  = field_map.get("full_street",  "full_street_name")
    lat_field = field_map.get("lat", "lat")
    lon_field = field_map.get("lon", "lon")

    BATCH = 1000
    inserted = 0

    batch = []
    for row in rows:
        fa = str(row.get(fa_field) or "").strip().upper()
        lat_raw = row.get(lat_field)
        lon_raw = row.get(lon_field)
        if not fa or fa == "NO ADDRESS" or not lat_raw or not lon_raw:
            continue
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (TypeError, ValueError):
            continue

        batch.append((
            fa,
            str(row.get(hn_field) or "").strip() or None,
            str(row.get(sn_field) or "").strip() or None,
            str(row.get(st_field) or "").strip() or None,
            str(row.get(pd_field) or "").strip() or None,
            str(row.get(fs_field) or "").strip() or None,
            lat, lon, city_value, source_name,
        ))

        if len(batch) >= BATCH:
            _insert_batch(batch)
            inserted += len(batch)
            batch = []
            log.info("  Inserted %d so far…", inserted)

    if batch:
        _insert_batch(batch)
        inserted += len(batch)

    return inserted


def _insert_batch(batch):
    with db() as conn:
        cur = conn.cursor()
        from psycopg2.extras import execute_values
        execute_values(cur, """
            INSERT INTO address_db
                (full_address, house_number, street_name, street_type,
                 predirectional, full_street, lat, lon, city, source)
            VALUES %s
            ON CONFLICT DO NOTHING
        """, batch)


def load_socrata(cfg: dict, city_value: str) -> int:
    url    = cfg["url"]
    params = cfg.get("params", {})
    fields = cfg.get("fields", {})
    log.info("Downloading Socrata: %s", url)
    resp = requests.get(url, params=params, timeout=120)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = list(reader)
    log.info("  Downloaded %d rows", len(rows))
    return _load_records(rows, fields, city_value, "socrata")


def load_arcgis(cfg: dict, city_value: str) -> int:
    url    = cfg["url"]
    params = dict(cfg.get("params", {}))
    params.setdefault("f", "json")
    fields = cfg.get("fields", {})
    rows = []
    offset = 0
    batch_size = int(params.get("resultRecordCount", 2000))
    log.info("Downloading ArcGIS: %s", url)
    while True:
        params["resultOffset"] = offset
        resp = requests.get(url, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", [])
        if not features:
            break
        for f in features:
            attrs = f.get("attributes", {})
            geom  = f.get("geometry", {})
            # Coordinates may be in attributes (X/Y) or in geometry
            if not attrs.get(fields.get("lat", "Y")) and geom:
                attrs[fields.get("lat", "Y")] = geom.get("y")
                attrs[fields.get("lon", "X")] = geom.get("x")
            rows.append(attrs)
        offset += len(features)
        log.info("  Fetched %d records so far…", offset)
        if len(features) < batch_size:
            break
    return _load_records(rows, fields, city_value, "arcgis")


def load_csv(cfg: dict, city_value: str) -> int:
    url_or_path = cfg["url"]
    fields = cfg.get("fields", {})
    if url_or_path.startswith("http"):
        log.info("Downloading CSV: %s", url_or_path)
        resp = requests.get(url_or_path, timeout=120)
        resp.raise_for_status()
        text = resp.text
    else:
        log.info("Reading local CSV: %s", url_or_path)
        text = Path(url_or_path).read_text()
    rows = list(csv.DictReader(io.StringIO(text)))
    log.info("  Loaded %d rows", len(rows))
    return _load_records(rows, fields, city_value, "csv")


def load_city_db(db_cfg: dict, city_label: Optional[str] = None):
    source     = db_cfg.get("source", "socrata")
    city_value = city_label or db_cfg.get("city_value", "")
    _clear_city(city_value)

    if source == "socrata":
        n = load_socrata(db_cfg, city_value)
    elif source == "arcgis":
        n = load_arcgis(db_cfg, city_value)
    elif source == "csv":
        n = load_csv(db_cfg, city_value)
    else:
        log.error("Unknown source type: %s", source)
        return

    log.info("✓ Loaded %d addresses for city=%r", n, city_value)


def main():
    parser = argparse.ArgumentParser(description="Load city address database")
    parser.add_argument("--all",   action="store_true", help="Load primary + additional_cities")
    parser.add_argument("--clear", action="store_true", help="Clear all address_db records first")
    parser.add_argument("--city",  help="City slug to load (default: from CITY_CONFIG env)")
    args = parser.parse_args()

    _ensure_schema()

    if args.clear:
        with db() as conn:
            conn.cursor().execute("TRUNCATE address_db RESTART IDENTITY")
        log.info("Cleared all address_db records.")

    from app.config import CITY
    if not CITY:
        log.error("No city config loaded. Set CITY_CONFIG env var or add data/cities/*/config.yaml")
        sys.exit(1)

    # Load primary city
    primary_db = CITY.get("address_db")
    if primary_db:
        log.info("Loading primary city: %s", CITY.get("name"))
        load_city_db(primary_db)
    else:
        log.warning("No address_db config in primary city config.")

    # Load additional cities if requested
    if args.all:
        for extra in CITY.get("additional_cities", []):
            log.info("Loading additional city: %s", extra.get("name", "?"))
            load_city_db(extra, city_label=extra.get("city_value") or extra.get("name"))

    # Final count
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT city, count(*) FROM address_db GROUP BY city ORDER BY count DESC")
        log.info("address_db totals:")
        for row in cur.fetchall():
            log.info("  %-20s %d addresses", row["city"], row["count"])


if __name__ == "__main__":
    main()
