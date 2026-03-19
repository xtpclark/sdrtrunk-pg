#!/usr/bin/env python3
"""
Fetch Norfolk Police incident reports from data.norfolk.gov (Socrata API)
and load into crime_reports table for cross-correlation with scanner calls.

Adapted from hamptonroads_heat/fetch_crime_data.py.

Usage:
    python scripts/fetch_crime_data.py [--days 7] [--dry-run]

Runs daily via cron to keep the blotter current.
Cross-correlation happens in cross_correlate_incidents() or via SQL.
"""
import sys, argparse, re, time, json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import DATABASE_URL
from app.db import db

# Norfolk Open Data — Police Incident Reports (Socrata)
SOCRATA_URL = "https://data.norfolk.gov/resource/r7bn-2egr.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Hampton Roads bbox for geocode validation
BBOX = {"sw_lat": 36.5, "sw_lon": -76.9, "ne_lat": 37.3, "ne_lon": -75.9}

_geocode_cache = {}
_last_geocode = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS crime_reports (
    id              bigserial PRIMARY KEY,
    inci_id         text UNIQUE NOT NULL,
    offense         text,
    offense_cat     text,       -- normalized category
    address         text,
    neighborhood    text,
    district        text,
    zone            text,
    occurred_at     timestamptz,
    lat             float,
    lon             float,
    geom            geometry(Point, 4326),
    fetched_at      timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cr_occurred_idx ON crime_reports (occurred_at);
CREATE INDEX IF NOT EXISTS cr_geom_idx     ON crime_reports USING gist (geom);
CREATE INDEX IF NOT EXISTS cr_cat_idx      ON crime_reports (offense_cat);
"""

# Offense → normalized category (same logic as hamptonroads_heat)
def categorize(offense: str) -> str:
    o = offense.upper()
    if any(x in o for x in ['ASSAULT', 'BATTERY', 'FIGHT']):
        return 'assault'
    if any(x in o for x in ['ROBBERY', 'STRONGARM']):
        return 'robbery'
    if any(x in o for x in ['BURGLARY', 'BREAK']):
        return 'burglary'
    if any(x in o for x in ['LARCENY', 'THEFT', 'SHOPLI']):
        return 'theft'
    if any(x in o for x in ['AUTO', 'VEHICLE', 'CAR']):
        return 'vehicle'
    if any(x in o for x in ['DRUG', 'NARCOT', 'OVERDOSE']):
        return 'drugs'
    if any(x in o for x in ['SHOOT', 'GUNSHOT', 'FIREARM']):
        return 'shooting'
    if any(x in o for x in ['HOMICIDE', 'MURDER', 'MANSLAUGHTER']):
        return 'homicide'
    if any(x in o for x in ['RAPE', 'SEXUAL', 'MOLEST']):
        return 'sexual_assault'
    if any(x in o for x in ['VANDAL', 'DAMAGE', 'GRAFFITI']):
        return 'vandalism'
    if any(x in o for x in ['DOMESTIC']):
        return 'domestic'
    if any(x in o for x in ['TRAFFIC', 'HIT', 'DUI', 'DWI']):
        return 'traffic'
    if any(x in o for x in ['TRESPASS']):
        return 'trespass'
    return 'other'


def parse_occurred_at(date_str: str, hour_str: str) -> datetime | None:
    """Parse date_occu + hour_occu into a timezone-aware datetime."""
    try:
        date = datetime.fromisoformat(date_str).date()
        hour_str = str(hour_str).replace('.0', '').zfill(4)
        hour = int(hour_str[:2])
        minute = int(hour_str[2:])
        dt = datetime(date.year, date.month, date.day, min(hour, 23), min(minute, 59),
                      tzinfo=timezone.utc)
        # Adjust for Eastern time (UTC-5 standard, UTC-4 DST) — approximate
        dt = dt.replace(tzinfo=None)
        import pytz
        eastern = pytz.timezone('America/New_York')
        dt = eastern.localize(dt)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def geocode(address: str) -> tuple[float, float] | None:
    global _last_geocode
    key = address.lower().strip()
    if key in _geocode_cache:
        return _geocode_cache[key]

    elapsed = time.monotonic() - _last_geocode
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_geocode = time.monotonic()

    # Clean address — normalize common abbreviations
    clean = re.sub(r'\b(AVENUE|AVE)\b', 'Ave', address, flags=re.I)
    clean = re.sub(r'\b(STREET|ST)\b', 'St', clean, flags=re.I)
    clean = re.sub(r'\b(ROAD|RD)\b', 'Rd', clean, flags=re.I)
    clean = re.sub(r'\b(BOULEVARD|BLVD)\b', 'Blvd', clean, flags=re.I)
    query = f"{clean}, Norfolk, VA"

    params = {
        "q": query, "format": "json", "limit": 3,
        "countrycodes": "us",
        "viewbox": f"{BBOX['sw_lon']},{BBOX['ne_lat']},{BBOX['ne_lon']},{BBOX['sw_lat']}",
        "bounded": 1,
    }
    headers = {"User-Agent": "sdrtrunk-pg-crime-blotter/1.0"}

    try:
        r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        results = r.json()
        for res in results:
            lat, lon = float(res["lat"]), float(res["lon"])
            if BBOX["sw_lat"] <= lat <= BBOX["ne_lat"] and BBOX["sw_lon"] <= lon <= BBOX["ne_lon"]:
                _geocode_cache[key] = (lat, lon)
                return (lat, lon)
    except Exception as e:
        print(f"  Geocode error for {address!r}: {e}")

    _geocode_cache[key] = None
    return None


def fetch_incidents(days: int = 7) -> list[dict]:
    """Fetch recent incidents from Socrata API."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00.000")
    params = {
        "$where": f"date_occu >= '{since}'",
        "$limit": 10000,
        "$order": "date_occu DESC",
    }
    print(f"Fetching incidents since {since}...")
    r = requests.get(SOCRATA_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    print(f"Got {len(data)} incidents from API")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Ensure schema exists
    if not args.dry_run:
        with db() as conn:
            conn.cursor().execute(SCHEMA)
        print("Schema ready.")

    # Fetch from API
    raw = fetch_incidents(days=args.days)

    inserted = skipped = geocoded = 0

    for row in raw:
        inci_id = row.get("inci_id", "")
        if not inci_id:
            continue

        offense = row.get("offense", "UNKNOWN")
        streetno = str(row.get("streetno", "")).replace(".0", "").strip()
        street = str(row.get("street", "")).strip()
        address = f"{streetno} {street}".strip() if streetno else street
        neighborhood = row.get("neighborhd", "")
        district = row.get("district", "")
        zone = row.get("zone", "")
        offense_cat = categorize(offense)
        occurred_at = parse_occurred_at(row.get("date_occu", ""), row.get("hour_occu", "0"))

        # Geocode
        coords = geocode(address) if address else None
        lat, lon = (coords[0], coords[1]) if coords else (None, None)
        if coords:
            geocoded += 1

        if args.dry_run:
            print(f"  {inci_id} | {offense_cat:12} | {address} | {lat:.4f},{lon:.4f}" if coords
                  else f"  {inci_id} | {offense_cat:12} | {address} | no geocode")
            continue

        with db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO crime_reports
                        (inci_id, offense, offense_cat, address, neighborhood,
                         district, zone, occurred_at, lat, lon, geom)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s IS NOT NULL
                                 THEN ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                                 ELSE NULL END)
                    ON CONFLICT (inci_id) DO UPDATE SET
                        lat=EXCLUDED.lat, lon=EXCLUDED.lon, geom=EXCLUDED.geom,
                        fetched_at=now()
                """, (inci_id, offense, offense_cat, address, neighborhood,
                      district, zone, occurred_at, lat, lon,
                      lon, lon, lat))  # geom uses lon,lat for MakePoint
                inserted += 1
            except Exception as e:
                print(f"  Insert error {inci_id}: {e}")
                skipped += 1

    print(f"\nDone: {inserted} upserted, {geocoded} geocoded, {skipped} errors")

    if not args.dry_run:
        # Quick cross-correlation summary
        with db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT cr.offense_cat, count(*) as blotter_count,
                       count(DISTINCT c.id) as scanner_calls_nearby
                FROM crime_reports cr
                LEFT JOIN calls c ON
                    c.received_at BETWEEN cr.occurred_at - interval '2 hours'
                                      AND cr.occurred_at + interval '2 hours'
                    AND cr.geom IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM call_entities e
                        WHERE e.call_id = c.id
                          AND e.geom IS NOT NULL
                          AND ST_DWithin(cr.geom::geography, e.geom::geography, 500)
                    )
                WHERE cr.occurred_at > now() - interval '24 hours'
                GROUP BY cr.offense_cat
                ORDER BY blotter_count DESC
            """)
            rows = cur.fetchall()
            if rows:
                print("\n=== Cross-Correlation (last 24h) ===")
                print(f"{'Category':<20} {'Blotter':>8} {'Scanner Nearby':>15}")
                for r in rows:
                    print(f"{r['offense_cat']:<20} {r['blotter_count']:>8} {r['scanner_calls_nearby']:>15}")


if __name__ == "__main__":
    main()
