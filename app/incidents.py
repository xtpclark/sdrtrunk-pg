"""
Incident threading engine.

Called after entity extraction + geocoding completes for each call.
Determines if a call should open a new incident or join an existing one.

Logic:
1. If call has a geocoded address entity → try to open/extend incident
   a. Check for active incident within 500m (last 30 min) → join (geo_proximity)
   b. Check for active incident where this radio_id already participated → join (anchor_radio)
   c. Check for active incident on same TG within 5 min → join (tg_window)
   d. None found → create new incident (anchor)
2. Any call (with or without address) whose radio_id already appears in an
   active incident → join that incident (anchor_radio)

Background closer: incidents with no activity for 15 min → status='closed'.
"""

import logging
from datetime import datetime, timezone

from app.db import db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def _find_incident_by_geo(lat: float, lon: float, within_meters: int = 500) -> int | None:
    """
    Return the id of the nearest active incident within *within_meters* of
    (lat, lon) that has had activity in the last 30 minutes, or None.
    """
    sql = """
        SELECT i.id
        FROM incidents i
        WHERE i.status = 'active'
          AND i.location IS NOT NULL
          AND ST_DWithin(
              i.location::geography,
              ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
              %s
          )
          AND i.last_activity > now() - interval '30 minutes'
        ORDER BY ST_Distance(
            i.location::geography,
            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
        )
        LIMIT 1
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql, (lon, lat, within_meters, lon, lat))
        row = cur.fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Radio-ID helpers
# ---------------------------------------------------------------------------

def _find_incident_by_radio(radio_id: str) -> int | None:
    """
    Return the id of an active incident that *radio_id* has already
    participated in (within the last 30 min of activity), or None.
    """
    if not radio_id:
        return None
    sql = """
        SELECT ic.incident_id
        FROM incident_calls ic
        JOIN incidents i ON i.id = ic.incident_id
        WHERE ic.radio_id = %s
          AND i.status = 'active'
          AND i.last_activity > now() - interval '30 minutes'
        ORDER BY i.last_activity DESC
        LIMIT 1
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql, (radio_id,))
        row = cur.fetchone()
    return row["incident_id"] if row else None


# ---------------------------------------------------------------------------
# TG-window helper
# ---------------------------------------------------------------------------

def _find_incident_by_tg(tg: int, ts: datetime, window_minutes: int = 5) -> int | None:
    """
    Return an active incident that had call activity on *tg* within
    *window_minutes* of *ts*, or None.
    """
    if tg is None:
        return None
    sql = """
        SELECT i.id
        FROM incidents i
        JOIN incident_calls ic ON ic.incident_id = i.id
        JOIN calls c ON c.id = ic.call_id
        WHERE i.status = 'active'
          AND c.tg = %s
          AND c.ts BETWEEN %s - (%s || ' minutes')::interval
                       AND %s + (%s || ' minutes')::interval
          AND i.last_activity > now() - interval '30 minutes'
        ORDER BY i.last_activity DESC
        LIMIT 1
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql, (tg, ts, window_minutes, ts, window_minutes))
        row = cur.fetchone()
    return row["id"] if row else None


# ---------------------------------------------------------------------------
# Incident create / join
# ---------------------------------------------------------------------------

def _create_incident(
    call_id: int,
    address: str,
    lat: float,
    lon: float,
    category: str | None,
    ts: datetime,
    radio_id: str | None,
) -> int:
    """
    Create a new incident anchored to *call_id*. Returns the new incident id.
    """
    sql_incident = """
        INSERT INTO incidents
            (anchor_call_id, address, location, category,
             opened_at, last_activity, status, call_count, unit_count)
        VALUES
            (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s,
             %s, %s, 'active', 1, 1)
        RETURNING id
    """
    sql_ic = """
        INSERT INTO incident_calls (incident_id, call_id, radio_id, join_reason)
        VALUES (%s, %s, %s, 'anchor')
        ON CONFLICT (incident_id, call_id) DO NOTHING
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql_incident, (call_id, address, lon, lat, category, ts, ts))
        incident_id = cur.fetchone()["id"]
        cur.execute(sql_ic, (incident_id, call_id, radio_id))

    log.info(
        "Created incident %d for call %d @ %s (category=%s)",
        incident_id, call_id, address, category,
    )
    return incident_id


def _join_incident(
    incident_id: int,
    call_id: int,
    radio_id: str | None,
    reason: str,
    ts: datetime | None = None,
) -> None:
    """
    Add *call_id* to an existing incident and update aggregate counters.
    """
    if ts is None:
        ts = datetime.now(timezone.utc)

    sql_ic = """
        INSERT INTO incident_calls (incident_id, call_id, radio_id, join_reason, joined_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (incident_id, call_id) DO NOTHING
    """
    # Recount distinct radio_ids (including this new one) as unit_count
    sql_update = """
        UPDATE incidents
        SET last_activity = GREATEST(last_activity, %s),
            call_count    = (
                SELECT count(*) FROM incident_calls WHERE incident_id = %s
            ),
            unit_count    = (
                SELECT count(DISTINCT radio_id)
                FROM incident_calls
                WHERE incident_id = %s AND radio_id IS NOT NULL
            )
        WHERE id = %s
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql_ic, (incident_id, call_id, radio_id, reason, ts))
        cur.execute(sql_update, (ts, incident_id, incident_id, incident_id))

    log.info(
        "Joined call %d to incident %d (reason=%s, radio_id=%s)",
        call_id, incident_id, reason, radio_id,
    )


# ---------------------------------------------------------------------------
# Stale-incident closer (called from alert worker loop)
# ---------------------------------------------------------------------------

def close_stale_incidents() -> int:
    """
    Close any active incident whose last_activity is more than 15 minutes ago.
    Returns the number of incidents closed.
    """
    sql = """
        UPDATE incidents
        SET status    = 'closed',
            closed_at = now()
        WHERE status = 'active'
          AND last_activity < now() - interval '15 minutes'
        RETURNING id
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        closed = cur.fetchall()

    count = len(closed)
    if count:
        ids = [r["id"] for r in closed]
        log.info("Closed %d stale incident(s): %s", count, ids)
    return count


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_call_for_incidents(call_id: int) -> None:
    """
    Main entry point. Called after geocoding completes for *call_id*.

    Determines whether the call should open a new incident or join an existing
    one, applying the following priority order for calls with a geocoded address:

      1. geo_proximity  — active incident within 500 m (last 30 min)
      2. anchor_radio   — caller's radio_id is already in an active incident
      3. tg_window      — same TG with activity within ±5 min
      4. anchor         — create new incident (call has address and none of above matched)

    For calls *without* a geocoded address, only anchor_radio is checked.
    """
    # ------------------------------------------------------------------
    # Fetch call metadata + best geocoded address entity
    # ------------------------------------------------------------------
    sql_call = """
        SELECT c.id, c.tg, c.radio_id, c.ts,
               t.category,
               ce.value   AS address,
               ce.lat,
               ce.lon
        FROM calls c
        LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
        LEFT JOIN LATERAL (
            SELECT value, lat, lon
            FROM call_entities
            WHERE call_id = c.id
              AND entity_type = 'address'
              AND lat IS NOT NULL
              AND lon IS NOT NULL
            ORDER BY confidence DESC, id
            LIMIT 1
        ) ce ON true
        WHERE c.id = %s
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql_call, (call_id,))
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

    has_geo = lat is not None and lon is not None

    # ------------------------------------------------------------------
    # Path A: call has a geocoded address → full matching cascade
    # ------------------------------------------------------------------
    if has_geo:
        # 1. Geo proximity
        incident_id = _find_incident_by_geo(lat, lon)
        if incident_id:
            _join_incident(incident_id, call_id, radio_id, "geo_proximity", ts)
            return

        # 2. Anchor radio
        if radio_id:
            incident_id = _find_incident_by_radio(radio_id)
            if incident_id:
                _join_incident(incident_id, call_id, radio_id, "anchor_radio", ts)
                return

        # 3. TG window
        incident_id = _find_incident_by_tg(tg, ts)
        if incident_id:
            _join_incident(incident_id, call_id, radio_id, "tg_window", ts)
            return

        # 4. Create new incident (anchor)
        _create_incident(call_id, address, lat, lon, category, ts, radio_id)
        return

    # ------------------------------------------------------------------
    # Path B: no geocoded address — only join via radio_id
    # ------------------------------------------------------------------
    if radio_id:
        incident_id = _find_incident_by_radio(radio_id)
        if incident_id:
            _join_incident(incident_id, call_id, radio_id, "anchor_radio", ts)
            return

    # No match and no address → not part of any incident
    log.debug("call %d: no incident match (no geo, no matching radio_id)", call_id)
