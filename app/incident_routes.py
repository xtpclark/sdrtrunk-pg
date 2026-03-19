"""
Incident API endpoints.

  GET /api/incidents                     — list incidents (status, limit, offset)
  GET /api/incidents/<id>                — incident detail with calls, transcripts, units
  GET /api/incidents/<id>/audio_playlist — ordered list of call audio URLs
"""

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request, abort

from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("incidents", __name__)


@bp.route("/api/incidents", methods=["GET"])
def list_incidents():
    status = request.args.get("status", "active")   # active | closed | all
    limit  = min(request.args.get("limit", 50, type=int), 500)
    offset = request.args.get("offset", 0, type=int)

    conditions = []
    params = []

    if status != "all":
        conditions.append("i.status = %s")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                i.id,
                i.anchor_call_id,
                i.address,
                ST_X(i.location) AS lon,
                ST_Y(i.location) AS lat,
                i.category,
                i.opened_at,
                i.closed_at,
                i.last_activity,
                i.status,
                i.call_count,
                i.unit_count,
                i.summary
            FROM incidents i
            {where}
            ORDER BY i.last_activity DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    # Serialize datetimes
    for r in rows:
        for k in ("opened_at", "closed_at", "last_activity"):
            if r[k]:
                r[k] = r[k].isoformat()

    return jsonify({"incidents": rows, "count": len(rows), "offset": offset})


@bp.route("/api/incidents/<int:incident_id>", methods=["GET"])
def get_incident(incident_id):
    with db() as conn:
        cur = conn.cursor()

        # Incident header
        cur.execute(
            """
            SELECT
                i.id,
                i.anchor_call_id,
                i.address,
                ST_X(i.location) AS lon,
                ST_Y(i.location) AS lat,
                i.category,
                i.opened_at,
                i.closed_at,
                i.last_activity,
                i.status,
                i.call_count,
                i.unit_count,
                i.summary
            FROM incidents i
            WHERE i.id = %s
            """,
            (incident_id,),
        )
        row = cur.fetchone()
        if not row:
            abort(404)
        incident = dict(row)

        # All calls in this incident, ordered chronologically
        cur.execute(
            """
            SELECT
                ic.call_id,
                ic.radio_id,
                ic.joined_at,
                ic.join_reason,
                c.tg,
                c.ts,
                c.duration_sec,
                c.file_path,
                c.transcript,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                t.category
            FROM incident_calls ic
            JOIN calls c ON c.id = ic.call_id
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE ic.incident_id = %s
            ORDER BY c.ts ASC
            """,
            (incident_id,),
        )
        calls = [dict(r) for r in cur.fetchall()]

        # Distinct units
        cur.execute(
            """
            SELECT DISTINCT radio_id
            FROM incident_calls
            WHERE incident_id = %s
              AND radio_id IS NOT NULL
            ORDER BY radio_id
            """,
            (incident_id,),
        )
        units = [r["radio_id"] for r in cur.fetchall()]

    # Serialize datetimes
    for k in ("opened_at", "closed_at", "last_activity"):
        if incident[k]:
            incident[k] = incident[k].isoformat()

    for c in calls:
        for k in ("ts", "joined_at"):
            if c[k]:
                c[k] = c[k].isoformat()

    incident["calls"] = calls
    incident["units"] = units
    return jsonify(incident)


@bp.route("/api/incidents/<int:incident_id>/audio_playlist", methods=["GET"])
def audio_playlist(incident_id):
    """
    Return an ordered list of call audio URLs for sequential playback.
    """
    with db() as conn:
        cur = conn.cursor()

        # Verify incident exists
        cur.execute("SELECT id FROM incidents WHERE id = %s", (incident_id,))
        if not cur.fetchone():
            abort(404)

        cur.execute(
            """
            SELECT
                ic.call_id,
                ic.radio_id,
                ic.join_reason,
                c.ts,
                c.duration_sec,
                c.file_path,
                c.transcript,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag
            FROM incident_calls ic
            JOIN calls c ON c.id = ic.call_id
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE ic.incident_id = %s
              AND c.file_path IS NOT NULL
            ORDER BY c.ts ASC
            """,
            (incident_id,),
        )
        rows = cur.fetchall()

    playlist = []
    for r in rows:
        # Only include calls whose audio file exists on disk
        if not r["file_path"] or not Path(r["file_path"]).exists():
            continue
        playlist.append(
            {
                "call_id":    r["call_id"],
                "radio_id":   r["radio_id"],
                "join_reason": r["join_reason"],
                "ts":         r["ts"].isoformat() if r["ts"] else None,
                "duration":   r["duration_sec"],
                "alpha_tag":  r["alpha_tag"],
                "transcript": (r["transcript"] or "")[:300],
                "audio_url":  f"/api/calls/{r['call_id']}/audio",
            }
        )

    return jsonify(
        {
            "incident_id": incident_id,
            "track_count": len(playlist),
            "tracks": playlist,
        }
    )
