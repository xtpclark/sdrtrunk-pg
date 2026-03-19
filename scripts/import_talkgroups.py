#!/usr/bin/env python3
"""
Import a RadioReference CSV talkgroup export into the talkgroups table.

RadioReference CSV columns (typical export):
  Decimal, Hex, Alpha Tag, Mode, Description, Tag, Category

Usage:
    python scripts/import_talkgroups.py <csvfile> [--system-id SYSTEM_ID]

Example:
    python scripts/import_talkgroups.py hamptonroads.csv --system-id VA-HR-P25
"""

import argparse
import csv
import sys
from pathlib import Path

# Make sure the project root is on sys.path so app modules are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import db  # noqa: E402


def parse_args():
    parser = argparse.ArgumentParser(
        description="Import RadioReference CSV talkgroups into sdrtrunk-pg."
    )
    parser.add_argument("csvfile", help="Path to the RadioReference CSV export")
    parser.add_argument(
        "--system-id",
        default="",
        help="System ID to tag all imported talkgroups (e.g. VA-HR-P25)",
    )
    return parser.parse_args()


def import_talkgroups(csvfile: str, system_id: str) -> int:
    """
    Read csvfile and UPSERT into talkgroups.
    Returns the number of rows processed.
    """
    path = Path(csvfile)
    if not path.exists():
        print(f"ERROR: file not found: {csvfile}", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)

        # Normalise header names — RadioReference can vary capitalization
        fieldnames = [f.strip() for f in (reader.fieldnames or [])]
        reader.fieldnames = fieldnames

        for row in reader:
            # Skip blank/comment rows
            decimal_str = row.get("Decimal", "").strip()
            if not decimal_str or not decimal_str.isdigit():
                continue

            rows.append(
                {
                    "tg_decimal":  int(decimal_str),
                    "alpha_tag":   row.get("Alpha Tag", "").strip(),
                    "mode":        row.get("Mode", "").strip(),
                    "description": row.get("Description", "").strip(),
                    "tag":         row.get("Tag", "").strip(),
                    "category":    row.get("Category", "").strip(),
                    "system_id":   system_id,
                }
            )

    if not rows:
        print("No valid rows found in CSV.")
        return 0

    count = 0
    with db() as conn:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                """
                INSERT INTO talkgroups
                    (tg_decimal, alpha_tag, mode, description, tag, category, system_id)
                VALUES
                    (%(tg_decimal)s, %(alpha_tag)s, %(mode)s,
                     %(description)s, %(tag)s, %(category)s, %(system_id)s)
                ON CONFLICT (tg_decimal) DO UPDATE SET
                    alpha_tag   = EXCLUDED.alpha_tag,
                    mode        = EXCLUDED.mode,
                    description = EXCLUDED.description,
                    tag         = EXCLUDED.tag,
                    category    = EXCLUDED.category,
                    system_id   = COALESCE(NULLIF(EXCLUDED.system_id, ''), talkgroups.system_id)
                """,
                r,
            )
            count += 1

    return count


def main():
    args = parse_args()
    n = import_talkgroups(args.csvfile, args.system_id)
    print(f"{n} talkgroups imported.")


if __name__ == "__main__":
    main()
