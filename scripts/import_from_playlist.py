#!/usr/bin/env python3
"""
Import talkgroups directly from an SDRTrunk playlist XML file.
Extracts alias name, group, talkgroup decimal, and protocol.

Usage:
    python scripts/import_from_playlist.py /path/to/default.xml [--system-id SYSTEM_ID]
"""

import sys
import argparse
import xml.etree.ElementTree as ET
import psycopg2
import psycopg2.extras
from pathlib import Path

# Add parent dir to path so we can import app config
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import DATABASE_URL


def parse_playlist(xml_path):
    """Parse SDRTrunk playlist XML and extract talkgroup aliases."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    talkgroups = []

    for alias in root.findall('alias'):
        name = alias.get('name', '').strip()
        group = alias.get('group', '').strip()
        alias_list = alias.get('list', '').strip()

        # Find talkgroup id elements
        for id_elem in alias.findall('id'):
            if id_elem.get('type') == 'talkgroup':
                try:
                    tg_decimal = int(id_elem.get('value', 0))
                    protocol = id_elem.get('protocol', 'APCO25')

                    if tg_decimal > 0:
                        talkgroups.append({
                            'tg_decimal': tg_decimal,
                            'alpha_tag': name[:30] if name else f'TG {tg_decimal}',
                            'description': name,
                            'category': infer_category(group, name),
                            'tag': group,
                            'mode': protocol,
                            'system_id': alias_list,
                        })
                except (ValueError, TypeError):
                    continue

    return talkgroups


def infer_category(group, name):
    """Infer a broad category from the group/name strings."""
    g = (group + ' ' + name).lower()
    if any(x in g for x in ['police', 'pd ', ' pd', 'sheriff', 'so ', 'pmo', 'security', 'sec ']):
        return 'Police'
    if any(x in g for x in ['fire', 'fd ', ' fd', 'ems', 'rescue', 'medic', 'ambulance']):
        return 'Fire/EMS'
    if any(x in g for x in ['school', 'bus ', 'transit']):
        return 'Schools'
    if any(x in g for x in ['navy', 'naval', 'marine', 'military', 'army', 'air force', 'coast guard', 'uscg', 'jblm', 'nsa ', 'nsn ', 'nws ']):
        return 'Military'
    if any(x in g for x in ['airport', 'orf ', 'tower', 'atis']):
        return 'Airport'
    if any(x in g for x in ['interop', 'mutual aid', 'mcall', 'comlinc', 'orion']):
        return 'Interop/Mutual Aid'
    if any(x in g for x in ['transport', 'dot ', 'highway', 'traffic', 'road']):
        return 'Transportation'
    if any(x in g for x in ['water', 'sewer', 'waste', 'utility', 'public works', 'garage', 'facility']):
        return 'Public Works'
    if any(x in g for x in ['state police', 'vsp ']):
        return 'State Police'
    return 'Government'


def main():
    parser = argparse.ArgumentParser(description='Import talkgroups from SDRTrunk playlist XML')
    parser.add_argument('xml_file', help='Path to SDRTrunk playlist XML file')
    parser.add_argument('--system-id', help='Override system_id for all entries')
    parser.add_argument('--dry-run', action='store_true', help='Parse only, do not write to DB')
    args = parser.parse_args()

    xml_path = Path(args.xml_file)
    if not xml_path.exists():
        print(f"Error: {xml_path} not found")
        sys.exit(1)

    print(f"Parsing {xml_path}...")
    talkgroups = parse_playlist(xml_path)

    if args.system_id:
        for tg in talkgroups:
            tg['system_id'] = args.system_id

    # Deduplicate by tg_decimal — keep last occurrence
    seen = {}
    for tg in talkgroups:
        seen[tg['tg_decimal']] = tg
    unique = list(seen.values())

    print(f"Found {len(talkgroups)} total entries, {len(unique)} unique talkgroups")

    # Show category breakdown
    from collections import Counter
    cats = Counter(tg['category'] for tg in unique)
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")

    if args.dry_run:
        print("\nDry run — not writing to DB")
        for tg in sorted(unique, key=lambda x: x['tg_decimal'])[:20]:
            print(f"  {tg['tg_decimal']:6d}  {tg['alpha_tag']:<30}  {tg['category']}")
        if len(unique) > 20:
            print(f"  ... and {len(unique)-20} more")
        return

    # Write to DB
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    inserted = 0
    updated = 0

    for tg in unique:
        cur.execute("""
            INSERT INTO talkgroups (tg_decimal, alpha_tag, description, category, tag, mode, system_id)
            VALUES (%(tg_decimal)s, %(alpha_tag)s, %(description)s, %(category)s, %(tag)s, %(mode)s, %(system_id)s)
            ON CONFLICT (tg_decimal) DO UPDATE SET
                alpha_tag   = EXCLUDED.alpha_tag,
                description = EXCLUDED.description,
                category    = EXCLUDED.category,
                tag         = EXCLUDED.tag,
                mode        = EXCLUDED.mode,
                system_id   = EXCLUDED.system_id
        """, tg)

        if cur.rowcount == 1:
            inserted += 1
        else:
            updated += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\nDone: {inserted} inserted, {updated} updated")


if __name__ == '__main__':
    main()
