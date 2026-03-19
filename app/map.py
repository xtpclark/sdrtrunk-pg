"""
Map blueprint — serves the Leaflet map page and GeoJSON/stats endpoints.

  GET /map                — serve map.html template
  GET /map/heatmap        — GeoJSON of geocoded entities (last N minutes)
  GET /map/incidents      — GeoJSON of calls with metadata
  GET /map/stats          — dashboard stats (calls today, active TGs, recent alerts)
"""

import logging

from flask import Blueprint, jsonify, render_template, request

from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("map", __name__)


@bp.route("/map")
def map_page():
    return render_template("map.html")


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
            SELECT a.id, a.message, a.fired_at,
                   r.name AS rule_name, r.rule_type
            FROM alerts a
            LEFT JOIN alert_rules r ON r.id = a.rule_id
            ORDER BY a.fired_at DESC
            LIMIT 5
            """
        )
        recent_alerts = [
            {
                "id":        r["id"],
                "message":   r["message"],
                "fired_at":  r["fired_at"].isoformat() if r["fired_at"] else None,
                "rule_name": r["rule_name"],
                "rule_type": r["rule_type"],
            }
            for r in cur.fetchall()
        ]

    return jsonify(
        {
            "calls_today":    calls_today,
            "active_talkgroups": active_tgs,
            "last_call_ts":   last_call_ts,
            "recent_alerts":  recent_alerts,
        }
    )
