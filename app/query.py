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

from app.config import ARCHIVE_ROOT
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

    # Validate path is within ARCHIVE_ROOT — defense against tampered records
    try:
        path.resolve().relative_to(ARCHIVE_ROOT.resolve())
    except ValueError:
        log.error("Audio path outside ARCHIVE_ROOT, refusing: %s", path)
        abort(403)

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


@bp.route("/api/live_feed", methods=["GET"])
def live_feed():
    """
    GET /api/live_feed — last N calls with transcript, talkgroup name, category, etc.
    params: limit (default 50)
    """
    limit = min(request.args.get("limit", 50, type=int), 500)

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                c.id,
                c.ts AT TIME ZONE 'America/New_York' AS ts,
                c.tg,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                t.category,
                c.radio_id,
                c.duration_sec,
                c.transcript,
                c.file_path IS NOT NULL AND c.file_path != '' AS has_audio,
                ic.incident_id
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            LEFT JOIN LATERAL (
                SELECT incident_id
                FROM incident_calls
                WHERE call_id = c.id
                ORDER BY incident_id DESC
                LIMIT 1
            ) ic ON true
            ORDER BY c.ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        if r["ts"]:
            r["ts"] = r["ts"].isoformat()

    return jsonify(rows)


@bp.route("/api/threads", methods=["GET"])
def threads():
    """
    GET /api/threads — active + recent incidents, located + unlocated.
    ?minutes=N (default 120) — show incidents with activity in last N minutes.
    """
    minutes = request.args.get("minutes", 120, type=int)
    minutes = max(5, min(minutes, 10080))

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                i.id,
                i.address,
                i.category,
                i.call_count,
                i.unit_count,
                i.last_activity AT TIME ZONE 'America/New_York' AS last_activity,
                i.opened_at    AT TIME ZONE 'America/New_York' AS opened_at,
                i.status,
                i.has_location,
                CASE WHEN i.location IS NOT NULL
                    THEN ST_Y(i.location) ELSE NULL END AS lat,
                CASE WHEN i.location IS NOT NULL
                    THEN ST_X(i.location) ELSE NULL END AS lon,
                (
                    SELECT c.transcript
                    FROM incident_calls ic2
                    JOIN calls c ON c.id = ic2.call_id
                    WHERE ic2.incident_id = i.id
                      AND c.transcript IS NOT NULL
                    ORDER BY c.ts DESC
                    LIMIT 1
                ) AS last_transcript,
                (
                    SELECT coalesce(t2.alpha_tag, c2.tg::text)
                    FROM incident_calls ic3
                    JOIN calls c2 ON c2.id = ic3.call_id
                    LEFT JOIN talkgroups t2 ON t2.tg_decimal = c2.tg
                    WHERE ic3.incident_id = i.id
                    ORDER BY c2.ts DESC
                    LIMIT 1
                ) AS last_alpha_tag
            FROM incidents i
            WHERE i.last_activity > now() - (%s || ' minutes')::interval
            ORDER BY
                CASE WHEN i.status = 'active' THEN 0 ELSE 1 END,
                i.last_activity DESC
            LIMIT 150
            """,
            (str(minutes),),
        )
        rows = [dict(r) for r in cur.fetchall()]

    located   = []
    unlocated = []
    for r in rows:
        for k in ("last_activity", "opened_at"):
            if r[k]:
                r[k] = r[k].isoformat()
        if r["has_location"]:
            located.append(r)
        else:
            unlocated.append(r)

    return jsonify({"located": located, "unlocated": unlocated})


@bp.route("/api/incidents/<int:incident_id>/detail", methods=["GET"])
def incident_detail(incident_id):
    """
    GET /api/incidents/<id>/detail — full incident detail for click-through.
    """
    with db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                i.id,
                i.address,
                i.category,
                i.call_count,
                i.unit_count,
                i.status,
                i.has_location,
                i.opened_at    AT TIME ZONE 'America/New_York' AS opened_at,
                i.closed_at    AT TIME ZONE 'America/New_York' AS closed_at,
                i.last_activity AT TIME ZONE 'America/New_York' AS last_activity,
                CASE WHEN i.location IS NOT NULL
                    THEN ST_Y(i.location) ELSE NULL END AS lat,
                CASE WHEN i.location IS NOT NULL
                    THEN ST_X(i.location) ELSE NULL END AS lon
            FROM incidents i
            WHERE i.id = %s
            """,
            (incident_id,),
        )
        row = cur.fetchone()
        if not row:
            from flask import abort
            abort(404)
        incident = dict(row)

        for k in ("opened_at", "closed_at", "last_activity"):
            if incident[k]:
                incident[k] = incident[k].isoformat()

        cur.execute(
            """
            SELECT
                ic.call_id AS id,
                c.ts AT TIME ZONE 'America/New_York' AS ts,
                c.tg,
                coalesce(t.alpha_tag, c.tg::text) AS alpha_tag,
                ic.radio_id,
                c.duration_sec,
                c.transcript,
                c.file_path IS NOT NULL AND c.file_path != '' AS has_audio,
                ic.join_reason
            FROM incident_calls ic
            JOIN calls c ON c.id = ic.call_id
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE ic.incident_id = %s
            ORDER BY c.ts ASC
            """,
            (incident_id,),
        )
        calls = [dict(r) for r in cur.fetchall()]
        for c in calls:
            if c["ts"]:
                c["ts"] = c["ts"].isoformat()

        cur.execute(
            """
            SELECT DISTINCT radio_id
            FROM incident_calls
            WHERE incident_id = %s AND radio_id IS NOT NULL
            ORDER BY radio_id
            """,
            (incident_id,),
        )
        units = [r["radio_id"] for r in cur.fetchall()]

    incident["calls"] = calls
    incident["units"] = units
    return jsonify(incident)


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


@bp.route("/api/calls/search", methods=["GET"])
def semantic_search():
    """
    Semantic similarity search over call transcripts.

    GET /api/calls/search?q=structure+fire+on+main+street&limit=10&category=Fire/EMS

    Returns calls ranked by embedding similarity to the query.
    Falls back to keyword search if no embeddings available.
    """
    from app.embed import get_embedding

    q       = request.args.get("q", "").strip()
    limit   = min(int(request.args.get("limit", 10)), 50)
    category = request.args.get("category", "")

    if not q:
        return jsonify({"error": "q parameter required"}), 400

    # Try semantic search first
    embedding = get_embedding(q)

    if embedding:
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
        with db() as conn:
            cur = conn.cursor()
            cat_filter = "AND t.category = %(cat)s" if category else ""
            cur.execute(f"""
                SELECT c.id, c.tg, t.alpha_tag, t.category, t.system_id,
                       c.radio_id, c.ts, c.duration_sec, c.transcript,
                       c.file_path,
                       1 - (c.embedding <=> %(vec)s::vector) AS similarity
                FROM calls c
                LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
                WHERE c.embedding IS NOT NULL
                  AND c.transcript IS NOT NULL
                  AND c.transcript != ''
                  {cat_filter}
                ORDER BY c.embedding <=> %(vec)s::vector
                LIMIT %(limit)s
            """, {"vec": vec_str, "cat": category, "limit": limit})
            results = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "query": q,
            "mode": "semantic",
            "count": len(results),
            "results": results
        })

    # Fallback: keyword search
    with db() as conn:
        cur = conn.cursor()
        cat_filter = "AND t.category = %(cat)s" if category else ""
        cur.execute(f"""
            SELECT c.id, c.tg, t.alpha_tag, t.category, t.system_id,
                   c.radio_id, c.ts, c.duration_sec, c.transcript, c.file_path,
                   1.0 AS similarity
            FROM calls c
            LEFT JOIN talkgroups t ON t.tg_decimal = c.tg
            WHERE c.transcript ILIKE %(q)s
              {cat_filter}
            ORDER BY c.ts DESC
            LIMIT %(limit)s
        """, {"q": f"%{q}%", "cat": category, "limit": limit})
        results = [dict(r) for r in cur.fetchall()]

    return jsonify({
        "query": q,
        "mode": "keyword_fallback",
        "count": len(results),
        "results": results
    })
