"""
Query endpoints:
  GET /api/calls                  — search calls
  GET /api/calls/<id>             — single call detail with entities
  GET /api/calls/<id>/audio       — stream MP3
  GET /api/talkgroups             — list talkgroups with call counts
  GET /api/stats                  — call volume stats
"""

import logging
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, abort

from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("query", __name__)


@bp.route("/api/calls", methods=["GET"])
def list_calls():
    tg       = request.args.get("tg", type=int)
    category = request.args.get("category")
    date     = request.args.get("date")          # YYYY-MM-DD
    keyword  = request.args.get("keyword")       # ILIKE on transcript
    limit    = min(request.args.get("limit", 50, type=int), 1000)
    offset   = request.args.get("offset", 0, type=int)

    conditions = []
    params = []

    if tg is not None:
        conditions.append("c.tg = %s")
        params.append(tg)
    if category:
        conditions.append("t.category ILIKE %s")
        params.append(f"%{category}%")
    if date:
        conditions.append("(c.ts AT TIME ZONE 'America/New_York')::date = %s")
        params.append(date)
    if keyword:
        conditions.append("c.transcript ILIKE %s")
        params.append(f"%{keyword}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
                c.id,
                c.tg,
                c.system_id,
                c.radio_id,
                c.ts AT TIME ZONE 'America/New_York' AS ts_local,
                c.duration_sec,
                c.freq_hz,
                c.file_path,
                c.transcript,
                c.transcript IS NOT NULL AS has_transcript,
                c.embedding IS NOT NULL AS has_embedding,
                c.received_at,
                t.alpha_tag,
                t.description AS tg_description,
                t.category,
                t.tag
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            {where}
            ORDER BY c.ts DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    return jsonify({"calls": rows, "count": len(rows), "offset": offset})


@bp.route("/api/calls/<int:call_id>", methods=["GET"])
def get_call(call_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                c.*,
                t.alpha_tag,
                t.description AS tg_description,
                t.category,
                t.tag
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE c.id = %s
            """,
            (call_id,),
        )
        row = cur.fetchone()
        if not row:
            abort(404)

        cur.execute(
            """
            SELECT id, entity_type, value, lat, lon, confidence, created_at
            FROM call_entities
            WHERE call_id = %s
            ORDER BY id
            """,
            (call_id,),
        )
        entities = [dict(r) for r in cur.fetchall()]

    result = dict(row)
    result["entities"] = entities
    return jsonify(result)


@bp.route("/api/calls/<int:call_id>/audio", methods=["GET"])
def stream_audio(call_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT file_path FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()
        if not row:
            abort(404)

    path = Path(row["file_path"])
    if not path.exists():
        log.error("Audio file missing on disk: %s", path)
        abort(404)

    return send_file(str(path), mimetype="audio/mpeg", as_attachment=False)


@bp.route("/api/talkgroups", methods=["GET"])
def list_talkgroups():
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                t.tg_decimal,
                t.alpha_tag,
                t.description,
                t.category,
                t.tag,
                t.mode,
                t.system_id,
                count(c.id)  AS total_calls,
                max(c.ts)    AS last_call
            FROM talkgroups t
            LEFT JOIN calls c ON c.tg = t.tg_decimal
            GROUP BY t.tg_decimal, t.alpha_tag, t.description,
                     t.category, t.tag, t.mode, t.system_id
            ORDER BY total_calls DESC
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

    return jsonify({"talkgroups": rows, "count": len(rows)})


@bp.route("/api/stats", methods=["GET"])
def stats():
    with db() as conn:
        cur = conn.cursor()

        # Overall totals
        cur.execute("SELECT count(*) AS total FROM calls")
        total = cur.fetchone()["total"]

        cur.execute(
            """
            SELECT count(*) AS today
            FROM calls
            WHERE (ts AT TIME ZONE 'America/New_York')::date = current_date
            """
        )
        today = cur.fetchone()["today"]

        # Calls by category
        cur.execute(
            """
            SELECT coalesce(t.category, 'Unknown') AS category,
                   count(*) AS calls
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            GROUP BY t.category
            ORDER BY calls DESC
            """
        )
        by_category = [dict(r) for r in cur.fetchall()]

        # Top 10 busiest talkgroups
        cur.execute(
            """
            SELECT c.tg,
                   coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                   t.category,
                   count(*) AS calls
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            GROUP BY c.tg, t.alpha_tag, t.category
            ORDER BY calls DESC
            LIMIT 10
            """
        )
        busiest_talkgroups = [dict(r) for r in cur.fetchall()]

        # Calls per hour today (local time)
        cur.execute(
            """
            SELECT extract(hour FROM ts AT TIME ZONE 'America/New_York')::int AS hour,
                   count(*) AS calls
            FROM calls
            WHERE (ts AT TIME ZONE 'America/New_York')::date = current_date
            GROUP BY 1
            ORDER BY 1
            """
        )
        calls_per_hour_today = [dict(r) for r in cur.fetchall()]

    return jsonify(
        {
            "total_calls": total,
            "calls_today": today,
            "by_category": by_category,
            "busiest_talkgroups": busiest_talkgroups,
            "calls_per_hour_today": calls_per_hour_today,
        }
    )
