"""
Incident threading engine.

Called after entity extraction + geocoding completes for each call.
Determines if a call should open a new incident or join an existing one.

Logic:
1. Always try to find/create an incident for every call.
2. For calls with a geocoded address → full matching cascade:
   a. geo_proximity  — active incident within 500m (last 30 min)
   b. anchor_radio   — caller's radio_id already in active incident
   c. tg_window      — same TG with activity within ±5 min
   d. anchor         — create new incident WITH location
3. For calls WITHOUT geocoded address:
   a. anchor_radio   — join by radio_id
   b. tg_window      — join by TG window
   c. anchor         — create new unlocated incident
4. "Promotion": when joining an unlocated incident and the incoming call
   has a geocoded address → set location/address/has_location on incident.

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
    address: str | None,
    lat: float | None,
    lon: float | None,
    category: str | None,
    ts: datetime,
    radio_id: str | None,
) -> int:
    """
    Create a new incident anchored to *call_id*.
    Works with or without lat/lon/address.
    Returns the new incident id.
    """
    has_location = lat is not None and lon is not None

    if has_location:
        sql_incident = """
            INSERT INTO incidents
                (anchor_call_id, address, location, category,
                 opened_at, last_activity, status, call_count, unit_count,
                 has_location)
            VALUES
                (%s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s,
                 %s, %s, 'active', 1, 1, true)
            RETURNING id
        """
        sql_params = (call_id, address, lon, lat, category, ts, ts)
    else:
        sql_incident = """
            INSERT INTO incidents
                (anchor_call_id, address, location, category,
                 opened_at, last_activity, status, call_count, unit_count,
                 has_location)
            VALUES
                (%s, %s, NULL, %s,
                 %s, %s, 'active', 1, 1, false)
            RETURNING id
        """
        sql_params = (call_id, address, category, ts, ts)

    sql_ic = """
        INSERT INTO incident_calls (incident_id, call_id, radio_id, join_reason)
        VALUES (%s, %s, %s, 'anchor')
        ON CONFLICT (incident_id, call_id) DO NOTHING
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql_incident, sql_params)
        incident_id = cur.fetchone()["id"]
        cur.execute(sql_ic, (incident_id, call_id, radio_id))

    log.info(
        "Created incident %d for call %d @ %s (category=%s, has_location=%s)",
        incident_id, call_id, address or "unlocated", category, has_location,
    )
    return incident_id


def _promote_incident_location(
    incident_id: int, address: str, lat: float, lon: float
) -> None:
    """
    Promote an unlocated incident by setting its geographic location.
    Called when a call with a geocoded address joins an unlocated incident.
    """
    sql = """
        UPDATE incidents
        SET address      = %s,
            location     = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
            has_location = true
        WHERE id = %s
          AND has_location = false
    """
    with db() as conn:
        cur = conn.cursor()
        cur.execute(sql, (address, lon, lat, incident_id))

    log.info(
        "Promoted incident %d to location %s (%.5f, %.5f)",
        incident_id, address, lat, lon,
    )


def _join_incident(
    incident_id: int,
    call_id: int,
    radio_id: str | None,
    reason: str,
    ts: datetime | None = None,
    address: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> None:
    """
    Add *call_id* to an existing incident and update aggregate counters.
    If address/lat/lon provided and incident is unlocated → promote it.
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

    # Promote if we now have location data and incident didn't before
    if lat is not None and lon is not None and address:
        _promote_incident_location(incident_id, address, lat, lon)

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

    Every call is eligible to join or create an incident.

    For calls WITH geocoded address (full cascade):
      1. geo_proximity  — active incident within 500 m
      2. anchor_radio   — radio_id in active incident
      3. tg_window      — same TG within ±5 min
      4. anchor         — create new located incident

    For calls WITHOUT geocoded address:
      1. anchor_radio   — radio_id in active incident
      2. tg_window      — same TG within ±5 min
      3. anchor         — create new unlocated incident

    Promotion: joining an unlocated incident with a geocoded call
    → incident gets location/address/has_location promoted.
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
            _join_incident(incident_id, call_id, radio_id, "geo_proximity", ts,
                           address=address, lat=lat, lon=lon)
            return

        # 2. Anchor radio
        if radio_id:
            incident_id = _find_incident_by_radio(radio_id)
            if incident_id:
                _join_incident(incident_id, call_id, radio_id, "anchor_radio", ts,
                               address=address, lat=lat, lon=lon)
                return

        # 3. TG window
        incident_id = _find_incident_by_tg(tg, ts)
        if incident_id:
            _join_incident(incident_id, call_id, radio_id, "tg_window", ts,
                           address=address, lat=lat, lon=lon)
            return

        # 4. Create new located incident
        _create_incident(call_id, address, lat, lon, category, ts, radio_id)
        return

    # ------------------------------------------------------------------
    # Path B: no geocoded address — join or create unlocated incident
    # ------------------------------------------------------------------
    # 1. Anchor radio
    if radio_id:
        incident_id = _find_incident_by_radio(radio_id)
        if incident_id:
            _join_incident(incident_id, call_id, radio_id, "anchor_radio", ts)
            return

    # 2. TG window
    incident_id = _find_incident_by_tg(tg, ts)
    if incident_id:
        _join_incident(incident_id, call_id, radio_id, "tg_window", ts)
        return

    # 3. Create new unlocated incident
    _create_incident(call_id, None, None, None, category, ts, radio_id)
