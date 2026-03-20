"""
Map blueprint — serves the Leaflet map page and GeoJSON/stats endpoints.

  GET /map                — serve map.html template
  GET /map/heatmap        — GeoJSON of geocoded entities (last N minutes)
  GET /map/incidents      — GeoJSON of calls with metadata
  GET /map/stats          — dashboard stats (calls today, active TGs, recent alerts)
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from app.config import CITY_NAME, CITY_MAP_CENTER, CITY_MAP_ZOOM
from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("map", __name__)


@bp.route("/map")
def map_page():
    return render_template(
        "map.html",
        city_name=CITY_NAME,
        map_center=CITY_MAP_CENTER,
        map_zoom=CITY_MAP_ZOOM,
    )


@bp.route("/map/heatmap")
def heatmap():
    """
    GeoJSON FeatureCollection of geocoded call_entities from the last N minutes.
    Each feature carries weight=1 for the heatmap layer.
    """
    minutes = request.args.get("minutes", 60, type=int)
    minutes = max(1, min(minutes, 1440))  # clamp 1 min – 24 hours

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                ce.id,
                ce.value,
                ce.entity_type,
                ce.lat,
                ce.lon,
                ce.call_id,
                c.tg,
                c.ts,
                c.duration_sec,
                c.transcript,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                t.category
            FROM call_entities ce
            JOIN calls c  ON c.id = ce.call_id
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE ce.geom IS NOT NULL
              AND c.ts >= now() - (%s || ' minutes')::interval
            ORDER BY c.ts DESC
            """,
            (str(minutes),),
        )
        rows = cur.fetchall()

    features = []
    for r in rows:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r["lon"], r["lat"]],
                },
                "properties": {
                    "id":         r["id"],
                    "call_id":    r["call_id"],
                    "entity":     r["value"],
                    "type":       r["entity_type"],
                    "tg":         r["tg"],
                    "alpha_tag":  r["alpha_tag"],
                    "category":   r["category"],
                    "ts":         r["ts"].isoformat() if r["ts"] else None,
                    "duration":   r["duration_sec"],
                    "transcript": (r["transcript"] or "")[:200],
                    "weight":     1,
                },
            }
        )

    return jsonify({"type": "FeatureCollection", "features": features})


@bp.route("/map/incidents")
def incidents():
    """
    GeoJSON FeatureCollection of calls.
    Uses the entity with lowest call_entities.id as the representative point
    for calls that have been geocoded.
    Falls back to including calls without a location (point = null).

    Query params: date (YYYY-MM-DD), tg (int), category (str)
    """
    date_filter     = request.args.get("date")
    tg_filter       = request.args.get("tg", type=int)
    category_filter = request.args.get("category")

    conditions = []
    params     = []

    if date_filter:
        conditions.append("(c.ts AT TIME ZONE 'America/New_York')::date = %s")
        params.append(date_filter)
    if tg_filter is not None:
        conditions.append("c.tg = %s")
        params.append(tg_filter)
    if category_filter:
        conditions.append("t.category ILIKE %s")
        params.append(f"%{category_filter}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                c.id,
                c.tg,
                c.ts,
                c.duration_sec,
                c.transcript,
                c.radio_id,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                t.category,
                t.description AS tg_description,
                (
                    SELECT row_to_json(e)
                    FROM (
                        SELECT lat, lon
                        FROM call_entities
                        WHERE call_id = c.id
                          AND geom IS NOT NULL
                        ORDER BY id
                        LIMIT 1
                    ) e
                ) AS location
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            {where}
            ORDER BY c.ts DESC
            LIMIT 500
            """,
            params,
        )
        rows = cur.fetchall()

    features = []
    for r in rows:
        loc = r["location"]
        if loc and loc.get("lat") and loc.get("lon"):
            geometry = {
                "type": "Point",
                "coordinates": [loc["lon"], loc["lat"]],
            }
        else:
            geometry = None  # include call but no map pin

        features.append(
            {
                "type":     "Feature",
                "geometry": geometry,
                "properties": {
                    "id":              r["id"],
                    "tg":              r["tg"],
                    "alpha_tag":       r["alpha_tag"],
                    "category":        r["category"],
                    "tg_description":  r["tg_description"],
                    "ts":              r["ts"].isoformat() if r["ts"] else None,
                    "duration":        r["duration_sec"],
                    "radio_id":        r["radio_id"],
                    "transcript":      (r["transcript"] or "")[:300],
                },
            }
        )

    return jsonify({"type": "FeatureCollection", "features": features})


@bp.route("/map/incidents_geo")
def incidents_geo():
    """
    GeoJSON FeatureCollection of active incidents for the incident map layer.

    Query params:
      status (default 'active') — active | closed | all
      minutes (default 120)     — only incidents with last_activity within N minutes
    """
    status  = request.args.get("status", "active")
    minutes = request.args.get("minutes", 120, type=int)
    minutes = max(1, min(minutes, 10080))  # clamp 1 min – 1 week

    conditions = ["i.location IS NOT NULL",
                  "i.last_activity > now() - (%s || ' minutes')::interval"]
    params = [str(minutes)]

    if status != "all":
        conditions.append("i.status = %s")
        params.append(status)

    where = "WHERE " + " AND ".join(conditions)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                i.id,
                i.address,
                i.category,
                i.opened_at,
                i.last_activity,
                i.status,
                i.call_count,
                i.unit_count,
                ST_X(i.location) AS lon,
                ST_Y(i.location) AS lat
            FROM incidents i
            {where}
            ORDER BY i.last_activity DESC
            LIMIT 500
            """,
            params,
        )
        rows = cur.fetchall()

    features = []
    for r in rows:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type":        "Point",
                    "coordinates": [r["lon"], r["lat"]],
                },
                "properties": {
                    "id":            r["id"],
                    "address":       r["address"],
                    "category":      r["category"],
                    "opened_at":     r["opened_at"].isoformat() if r["opened_at"] else None,
                    "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
                    "status":        r["status"],
                    "call_count":    r["call_count"],
                    "unit_count":    r["unit_count"],
                },
            }
        )

    return jsonify({"type": "FeatureCollection", "features": features})


@bp.route("/map/stats")
def map_stats():
    """
    Dashboard stats:
      - calls_today
      - active_talkgroups (distinct TGs with calls in last hour)
      - last_call_ts
      - recent_alerts (last 5)
    """
    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT count(*) AS calls_today
            FROM calls
            WHERE (ts AT TIME ZONE 'America/New_York')::date = current_date
            """
        )
        calls_today = cur.fetchone()["calls_today"]

        cur.execute(
            """
            SELECT count(DISTINCT tg) AS active_tgs
            FROM calls
            WHERE ts >= now() - interval '1 hour'
            """
        )
        active_tgs = cur.fetchone()["active_tgs"]

        cur.execute(
            """
            SELECT max(ts) AS last_call FROM calls
            """
        )
        last_call_row = cur.fetchone()
        last_call_ts  = last_call_row["last_call"].isoformat() if last_call_row["last_call"] else None

        cur.execute(
            """
            SELECT a.id, a.message, a.fired_at, a.call_id,
                   r.name AS rule_name, r.rule_type
            FROM alerts a
            LEFT JOIN alert_rules r ON r.id = a.rule_id
            ORDER BY a.fired_at DESC
            LIMIT 5
            """
        )
        raw_alerts = cur.fetchall()

        # For each alert, resolve the talkgroup and most recent incident
        recent_alerts = []
        for r in raw_alerts:
            alert = {
                "id":        r["id"],
                "message":   r["message"],
                "fired_at":  r["fired_at"].isoformat() if r["fired_at"] else None,
                "rule_name": r["rule_name"],
                "rule_type": r["rule_type"],
                "call_id":   r["call_id"],
                "tg":        None,
                "alpha_tag": None,
                "incident_id": None,
            }

            # Resolve TG — from call (keyword alerts) or parse message (volume spike)
            if r["call_id"]:
                cur.execute("""
                    SELECT c.tg, t.alpha_tag FROM calls c
                    LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
                    WHERE c.id = %s
                """, (r["call_id"],))
                row = cur.fetchone()
                if row:
                    alert["tg"]        = row["tg"]
                    alert["alpha_tag"] = row["alpha_tag"]
            elif r["rule_type"] == "volume_spike":
                # Extract TG from message: "Volume spike on tg 612 (Police):..."
                import re
                m = re.search(r'tg (\d+)', r["message"])
                if m:
                    alert["tg"] = int(m.group(1))
                    cur.execute("SELECT alpha_tag FROM talkgroups WHERE tg_decimal = %s", (alert["tg"],))
                    tg_row = cur.fetchone()
                    if tg_row:
                        alert["alpha_tag"] = tg_row["alpha_tag"]

            # Find the most recent active incident involving this TG
            if alert["tg"]:
                cur.execute("""
                    SELECT i.id FROM incidents i
                    JOIN incident_calls ic ON ic.incident_id = i.id
                    JOIN calls c ON c.id = ic.call_id
                    WHERE c.tg = %s AND i.status = 'active'
                    ORDER BY i.last_activity DESC LIMIT 1
                """, (alert["tg"],))
                inc_row = cur.fetchone()
                if inc_row:
                    alert["incident_id"] = inc_row["id"]

            recent_alerts.append(alert)

        # ── Urgent incident ──────────────────────────────────────────────
        # Score = (fire_ems_bonus * 5) + calls_last_5min * 3 + unit_count
        # Only active incidents with activity in last 15 min qualify.
        cur.execute(
            """
            SELECT
                i.id,
                i.address,
                i.category,
                i.call_count,
                i.unit_count,
                i.status,
                i.opened_at AT TIME ZONE 'America/New_York' AS opened_at,
                i.last_activity AT TIME ZONE 'America/New_York' AS last_activity,
                CASE WHEN i.has_location THEN ST_Y(i.location) ELSE NULL END AS lat,
                CASE WHEN i.has_location THEN ST_X(i.location) ELSE NULL END AS lon,
                COALESCE((
                    SELECT COUNT(*) FROM incident_calls ic2
                    JOIN calls c2 ON c2.id = ic2.call_id
                    WHERE ic2.incident_id = i.id
                      AND c2.ts > now() - interval '5 minutes'
                ), 0) AS calls_last_5min,
                CASE WHEN i.category ILIKE '%fire%' OR i.category ILIKE '%ems%'
                     THEN 1 ELSE 0 END AS is_fire_ems,
                (
                    SELECT c3.transcript
                    FROM incident_calls ic3
                    JOIN calls c3 ON c3.id = ic3.call_id
                    WHERE ic3.incident_id = i.id AND c3.transcript IS NOT NULL
                    ORDER BY c3.ts DESC LIMIT 1
                ) AS last_transcript,
                (
                    SELECT COALESCE(t2.alpha_tag, c4.tg::text)
                    FROM incident_calls ic4
                    JOIN calls c4 ON c4.id = ic4.call_id
                    LEFT JOIN talkgroups t2 ON t2.tg_decimal = c4.tg
                    WHERE ic4.incident_id = i.id
                    ORDER BY c4.ts DESC LIMIT 1
                ) AS last_alpha_tag
            FROM incidents i
            WHERE i.status = 'active'
              AND i.last_activity > now() - interval '15 minutes'
              AND i.call_count <= 100
            ORDER BY
                (CASE WHEN i.category ILIKE '%fire%' OR i.category ILIKE '%ems%'
                      THEN 5 ELSE 0 END)
                + COALESCE((
                    SELECT COUNT(*) FROM incident_calls ic2
                    JOIN calls c2 ON c2.id = ic2.call_id
                    WHERE ic2.incident_id = i.id
                      AND c2.ts > now() - interval '5 minutes'
                  ), 0) * 3
                + i.unit_count
                DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        urgent_incident = None
        if row:
            urgent_incident = {
                "id":             row["id"],
                "address":        row["address"],
                "category":       row["category"],
                "call_count":     row["call_count"],
                "unit_count":     row["unit_count"],
                "calls_last_5min":row["calls_last_5min"],
                "is_fire_ems":    bool(row["is_fire_ems"]),
                "last_activity":  row["last_activity"].isoformat() if row["last_activity"] else None,
                "opened_at":      row["opened_at"].isoformat() if row["opened_at"] else None,
                "lat":            row["lat"],
                "lon":            row["lon"],
                "last_transcript":row["last_transcript"],
                "last_alpha_tag": row["last_alpha_tag"],
            }

        # ── Activity histogram (calls per 5-min bucket, last 2h) ────
        cur.execute("""
            SELECT
                date_trunc('hour', ts AT TIME ZONE 'America/New_York')
                  + floor(extract(minute FROM ts AT TIME ZONE 'America/New_York') / 5)
                    * interval '5 minutes' AS bucket,
                count(*) AS calls,
                count(*) FILTER (WHERE t.category ILIKE '%police%') AS police,
                count(*) FILTER (WHERE t.category ILIKE '%fire%' OR t.category ILIKE '%ems%') AS fire_ems
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE c.ts >= now() - interval '2 hours'
            GROUP BY bucket
            ORDER BY bucket
        """)
        histogram = [
            {
                "bucket": r["bucket"].isoformat(),
                "calls":  r["calls"],
                "police": r["police"],
                "fire_ems": r["fire_ems"],
            }
            for r in cur.fetchall()
        ]

        # ── Top talkgroups (last 2h) ──────────────────────────────
        cur.execute("""
            SELECT coalesce(t.alpha_tag, c.tg::text) AS tg,
                   t.category,
                   count(*) AS calls
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE c.ts >= now() - interval '2 hours'
            GROUP BY t.alpha_tag, c.tg, t.category
            ORDER BY calls DESC
            LIMIT 8
        """)
        top_tgs = [dict(r) for r in cur.fetchall()]

    return jsonify(
        {
            "calls_today":       calls_today,
            "active_talkgroups": active_tgs,
            "last_call_ts":      last_call_ts,
            "recent_alerts":     recent_alerts,
            "urgent_incident":   urgent_incident,
            "histogram":         histogram,
            "top_tgs":           top_tgs,
        }
    )
