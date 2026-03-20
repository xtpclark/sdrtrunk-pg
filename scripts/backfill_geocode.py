#!/usr/bin/env python3
"""
Backfill geocoding for all address entities that don't have lat/lon yet.
Also clears geocodes outside the configured city bbox.

Usage:
    python scripts/backfill_geocode.py [--dry-run]
"""
import sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import db
from app.geocode import geocode, _in_bbox

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # First: clear bad geocodes (outside Hampton Roads)
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, value, lat, lon FROM call_entities
            WHERE lat IS NOT NULL
              AND entity_type = 'address'
        """)
        bad = [(r["id"], r["value"]) for r in cur.fetchall()
               if not _in_bbox(r["lat"], r["lon"])]

    if bad:
        print(f"Clearing {len(bad)} out-of-bbox geocodes:")
        for eid, val in bad:
            print(f"  [{eid}] {val!r}")
        if not args.dry_run:
            with db() as conn:
                cur = conn.cursor()
                cur.executemany(
                    "UPDATE call_entities SET lat=NULL, lon=NULL, geom=NULL WHERE id=%s",
                    [(eid,) for eid, _ in bad]
                )

    # Now geocode everything with entity_type='address' and no lat
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, value FROM call_entities
            WHERE entity_type = 'address'
              AND lat IS NULL
            ORDER BY id
        """)
        todo = cur.fetchall()

    print(f"\nGeocoding {len(todo)} address entities...")
    geocoded = failed = 0

    for ent in todo:
        result = geocode(ent["value"])
        if result:
            lat, lon = result
            print(f"  ✓ {ent['value']!r} → ({lat:.5f}, {lon:.5f})")
            if not args.dry_run:
                with db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        UPDATE call_entities
                        SET lat  = %s, lon = %s,
                            geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                        WHERE id = %s
                    """, (lat, lon, lon, lat, ent["id"]))
            geocoded += 1
        else:
            print(f"  ✗ {ent['value']!r}")
            failed += 1

    print(f"\nDone: {geocoded} geocoded, {failed} not found in city bbox")

if __name__ == "__main__":
    main()
