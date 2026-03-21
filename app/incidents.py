"""
Incident threading engine — v2.

Fixes vs v1:
  - Advisory lock on (tg, 2-minute bucket) prevents race-condition duplicate anchors.
    All reads + write happen inside ONE db() context so the xact lock holds for the
    entire find-or-create decision.
  - geo_proximity now respects category (fire won't join police incident by location).
  - radio_id='0' and dispatcher IDs excluded from anchor_radio.
  - anchor_radio scoped to same category + call_count ≤ 100 guard.
  - tg_window tightened to ±3 min (was 5).
  - stale closer bumped to 20 min.
  - unit_count excludes radio_id='0'.
"""

import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone

from app.db import db

log = logging.getLogger(__name__)

# Dispatchers / base stations key across every TG — exclude from radio-based joining.
_SKIP_RADIO_IDS = frozenset({'0', 'None', ''})

# Per-TG threading locks — prevents duplicate-anchor race condition when the
# embed worker processes a backlog of calls on the same TG back-to-back.
_tg_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_tg_locks_mutex = threading.Lock()


def _get_tg_lock(tg: int) -> threading.Lock:
    """Return (or create) the per-TG mutex."""
    with _tg_locks_mutex:
        return _tg_locks[tg]


# ---------------------------------------------------------------------------
# All queries run inside the caller's cursor (single connection)
# ---------------------------------------------------------------------------

def _geo_find(cur, lat, lon, category):
    cat_sql = "AND i.category = %s" if category else ""
    params = [lon, lat, 500] + ([category] if category else []) + [lon, lat]
    cur.execute(f"""
        SELECT i.id FROM incidents i
        WHERE i.status='active'
          AND i.location IS NOT NULL
          AND ST_DWithin(i.location::geography,
              ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)
          AND i.last_activity > now() - interval '30 minutes'
          AND i.call_count <= 100
          {cat_sql}
        ORDER BY ST_Distance(i.location::geography,
            ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography)
        LIMIT 1
    """, params)
    row = cur.fetchone()
    return row["id"] if row else None


def _radio_find(cur, radio_id, category):
    if not radio_id or radio_id in _SKIP_RADIO_IDS:
        return None
    cat_sql = "AND i.category = %s" if category else ""
    params = [radio_id] + ([category] if category else [])
    cur.execute(f"""
        SELECT ic.incident_id FROM incident_calls ic
        JOIN incidents i ON i.id=ic.incident_id
        WHERE ic.radio_id=%s
          AND i.status='active'
          AND i.last_activity > now() - interval '30 minutes'
          AND i.call_count <= 100
          {cat_sql}
        ORDER BY i.last_activity DESC LIMIT 1
    """, params)
    row = cur.fetchone()
    return row["incident_id"] if row else None


def _tg_find(cur, tg, ts, window=3, call_lat=None, call_lon=None):
    """
    Find active incident with same TG activity within ±window minutes.

    If both the candidate incident and the incoming call have geocoded
    locations, verify they're within 1km. This prevents dispatch channels
    from merging sequential unrelated calls that happen
    to be on the same TG within the time window.
    """
    cur.execute("""
        SELECT i.id,
               CASE WHEN i.has_location THEN ST_Y(i.location) END AS inc_lat,
               CASE WHEN i.has_location THEN ST_X(i.location) END AS inc_lon
        FROM incidents i
        JOIN incident_calls ic ON ic.incident_id=i.id
        JOIN calls c ON c.id=ic.call_id
        WHERE i.status='active'
          AND c.tg=%s
          AND c.ts BETWEEN %s - (%s||' minutes')::interval
                       AND %s + (%s||' minutes')::interval
          AND i.last_activity > now() - interval '30 minutes'
          AND i.call_count <= 100
        ORDER BY i.last_activity DESC LIMIT 1
    """, (tg, ts, window, ts, window))
    row = cur.fetchone()
    if not row:
        return None

    # Geo guard: if both sides have locations, reject if >1km apart
    if call_lat is not None and row["inc_lat"] is not None:
        dist = _haversine(call_lat, call_lon, row["inc_lat"], row["inc_lon"])
        if dist > 1000:
            log.info("tg_window geo-reject: incident %d is %.0fm from new call (>1km)",
                     row["id"], dist)
            return None

    return row["id"]


def _haversine(lat1, lon1, lat2, lon2):
    """Distance in meters between two lat/lon points."""
    from math import radians, cos, sin, asin, sqrt
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    return 2 * asin(sqrt(h)) * 6371000


def _create(cur, call_id, address, lat, lon, category, ts, radio_id) -> int:
    """
    Create a new incident anchored to call_id.
    Uses ON CONFLICT DO NOTHING on the unique anchor_call_id constraint —
    if a race caused another worker to already anchor this call, we find
    that incident and join it instead of creating a duplicate.
    """
    has_loc = lat is not None and lon is not None
    if has_loc:
        cur.execute("""
            INSERT INTO incidents
                (anchor_call_id,address,location,category,opened_at,last_activity,
                 status,call_count,unit_count,has_location)
            VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,%s,%s,'active',1,1,true)
            ON CONFLICT (anchor_call_id) DO NOTHING
            RETURNING id
        """, (call_id, address, lon, lat, category, ts, ts))
    else:
        cur.execute("""
            INSERT INTO incidents
                (anchor_call_id,address,location,category,opened_at,last_activity,
                 status,call_count,unit_count,has_location)
            VALUES (%s,%s,NULL,%s,%s,%s,'active',1,1,false)
            ON CONFLICT (anchor_call_id) DO NOTHING
            RETURNING id
        """, (call_id, address, category, ts, ts))

    row = cur.fetchone()
    if row:
        # We won the race — new incident created
        incident_id = row["id"]
        cur.execute("""
            INSERT INTO incident_calls (incident_id,call_id,radio_id,join_reason)
            VALUES (%s,%s,%s,'anchor') ON CONFLICT (incident_id,call_id) DO NOTHING
        """, (incident_id, call_id, radio_id))
        log.info("Created incident %d call=%d @ %s cat=%s loc=%s",
                 incident_id, call_id, address or "unlocated", category, has_loc)
    else:
        # Lost the race — find the incident that won and join it
        cur.execute("SELECT id FROM incidents WHERE anchor_call_id=%s", (call_id,))
        existing = cur.fetchone()
        if existing:
            incident_id = existing["id"]
            cur.execute("""
                INSERT INTO incident_calls (incident_id,call_id,radio_id,join_reason)
                VALUES (%s,%s,%s,'anchor') ON CONFLICT (incident_id,call_id) DO NOTHING
            """, (incident_id, call_id, radio_id))
            log.info("Race resolved: call %d joined existing incident %d", call_id, incident_id)
        else:
            log.error("Race resolution failed for call %d — no anchor incident found", call_id)
            incident_id = -1

    return incident_id


def _join(cur, incident_id, call_id, radio_id, reason, ts, address=None, lat=None, lon=None):
    cur.execute("""
        INSERT INTO incident_calls (incident_id,call_id,radio_id,join_reason,joined_at)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (incident_id,call_id) DO NOTHING
    """, (incident_id, call_id, radio_id, reason, ts))
    cur.execute("""
        UPDATE incidents SET
            last_activity = GREATEST(last_activity, %s),
            call_count    = (SELECT count(*) FROM incident_calls WHERE incident_id=%s),
            unit_count    = (SELECT count(DISTINCT radio_id) FROM incident_calls
                             WHERE incident_id=%s AND radio_id IS NOT NULL AND radio_id NOT IN ('0',''))
        WHERE id=%s
    """, (ts, incident_id, incident_id, incident_id))
    # Update incident location:
    # - If unlocated → promote with this call's location (first pin on the map)
    # - If already located → update address text to the newest geocoded address
    #   (dispatch works multiple calls in sequence; the most recent address in
    #   the thread is usually the most specific/correct one)
    if lat is not None and lon is not None and address:
        cur.execute("""
            UPDATE incidents SET
                address      = %s,
                location     = ST_SetSRID(ST_MakePoint(%s,%s), 4326),
                has_location = true
            WHERE id = %s
        """, (address, lon, lat, incident_id))
    log.debug("Joined call %d → incident %d (%s)", call_id, incident_id, reason)


# ---------------------------------------------------------------------------
# Stale closer
# ---------------------------------------------------------------------------

def close_stale_incidents() -> int:
    """Close incidents with no activity for 20 minutes."""
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE incidents SET status='closed', closed_at=now()
            WHERE status='active'
              AND last_activity < now() - interval '20 minutes'
            RETURNING id
        """)
        closed = cur.fetchall()
    count = len(closed)
    if count:
        log.info("Closed %d stale incident(s): %s", count, [r["id"] for r in closed])
    return count


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_call_for_incidents(call_id: int) -> None:
    """
    Find or create an incident for call_id.
    All operations run inside a single DB connection protected by a
    PostgreSQL advisory transaction lock keyed on (tg, 2-min bucket).
    """
    # ── Step 1: fetch call metadata ─────────────────────────────────
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT c.id, c.tg, c.radio_id, c.ts,
                   t.category,
                   ce.value AS address, ce.lat, ce.lon
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal=c.tg
            LEFT JOIN LATERAL (
                SELECT value,lat,lon FROM call_entities
                WHERE call_id=c.id AND entity_type='address'
                  AND lat IS NOT NULL AND lon IS NOT NULL
                ORDER BY confidence DESC, id LIMIT 1
            ) ce ON true
            WHERE c.id=%s
        """, (call_id,))
        row = cur.fetchone()

    if not row:
        log.warning("process_call_for_incidents: call %d not found", call_id)
        return

    radio_id = row["radio_id"]
    tg       = row["tg"]
    ts       = row["ts"]
    category = row["category"]
    address  = row["address"]
    lat      = row["lat"]
    lon      = row["lon"]
    has_geo  = lat is not None and lon is not None

    # ── Step 2: acquire advisory lock + run find-or-create atomically ─
    tg_lock = _get_tg_lock(tg)

    with tg_lock, db() as conn:
        cur = conn.cursor()

        if has_geo:
            iid = _geo_find(cur, lat, lon, category)
            if iid:
                _join(cur, iid, call_id, radio_id, "geo_proximity", ts, address, lat, lon)
                return

            iid = _radio_find(cur, radio_id, category)
            if iid:
                _join(cur, iid, call_id, radio_id, "anchor_radio", ts, address, lat, lon)
                return

            iid = _tg_find(cur, tg, ts, call_lat=lat, call_lon=lon)
            if iid:
                _join(cur, iid, call_id, radio_id, "tg_window", ts, address, lat, lon)
                return

            _create(cur, call_id, address, lat, lon, category, ts, radio_id)

        else:
            iid = _radio_find(cur, radio_id, category)
            if iid:
                _join(cur, iid, call_id, radio_id, "anchor_radio", ts)
                return

            iid = _tg_find(cur, tg, ts, call_lat=lat, call_lon=lon)
            if iid:
                _join(cur, iid, call_id, radio_id, "tg_window", ts)
                return

            _create(cur, call_id, None, None, None, category, ts, radio_id)
