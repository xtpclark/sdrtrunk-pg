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


@bp.route("/api/incidents/<int:incident_id>/merge", methods=["POST"])
def merge_incident(incident_id):
    """
    POST /api/incidents/<id>/merge
    Concatenate all audio calls for an incident into a single MP3.

    Returns the merge job id immediately; poll GET /api/merge/<job_id>
    for status, then stream from GET /api/merge/<job_id>/audio when done.
    """
    from app.merge import run_merge, MERGE_ROOT
    import threading

    # Fetch incident to build a label
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.id, i.address, i.category,
                   i.opened_at AT TIME ZONE 'America/New_York' AS opened_at,
                   count(ic.call_id) AS call_count
            FROM incidents i
            JOIN incident_calls ic ON ic.incident_id = i.id
            JOIN calls c ON c.id = ic.call_id
            WHERE i.id = %s AND c.file_path != ''
            GROUP BY i.id, i.address, i.category, i.opened_at
            """,
            (incident_id,),
        )
        row = cur.fetchone()

    if not row:
        abort(404)

    if row["call_count"] == 0:
        return jsonify({"error": "No audio available for this incident"}), 400

    # Build a human-readable label: "incident_42_Police_2026-03-20"
    date_str  = row["opened_at"].strftime("%Y-%m-%d") if row["opened_at"] else "unknown"
    cat_slug  = (row["category"] or "Unknown").replace("/", "-").replace(" ", "")
    addr_slug = (row["address"] or "").replace(" ", "-")[:20]
    label     = f"incident_{incident_id}_{cat_slug}_{date_str}"
    if addr_slug:
        label += f"_{addr_slug}"

    # Create merge job using incident call_ids directly (not tg+window)
    with db() as conn:
        cur = conn.cursor()

        # Fetch ordered file paths for this incident
        cur.execute(
            """
            SELECT c.id, c.file_path
            FROM incident_calls ic
            JOIN calls c ON c.id = ic.call_id
            WHERE ic.incident_id = %s
              AND c.file_path IS NOT NULL
              AND c.file_path != ''
            ORDER BY c.ts ASC
            """,
            (incident_id,),
        )
        calls = cur.fetchall()

        valid_calls = [c for c in calls if Path(c["file_path"]).exists()]
        if not valid_calls:
            return jsonify({"error": "No audio files found on disk for this incident"}), 400

        # Insert a merge job record (reuse existing merge_jobs table)
        # Use tg=0 and window spanning all calls as a placeholder
        cur.execute(
            """
            INSERT INTO merge_jobs (label, tg, window_start, window_end, status)
            SELECT %s, 0,
                   min(c.ts) - interval '1 second',
                   max(c.ts) + interval '1 second',
                   'pending'
            FROM incident_calls ic
            JOIN calls c ON c.id = ic.call_id
            WHERE ic.incident_id = %s
            RETURNING id
            """,
            (label, incident_id),
        )
        job_id = cur.fetchone()["id"]

    # Run the merge in a background thread using the file list directly
    def _run_incident_merge(job_id, valid_calls, label):
        import subprocess, tempfile
        from app.config import MERGE_ROOT

        MERGE_ROOT.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        output_path = MERGE_ROOT / f"{job_id}_{safe_label}.mp3"

        try:
            with db() as conn:
                conn.cursor().execute(
                    "UPDATE merge_jobs SET status='running' WHERE id=%s", (job_id,))

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, prefix=f"merge_{job_id}_"
            ) as flist:
                for c in valid_calls:
                    escaped = str(c["file_path"]).replace("'", "'\\''")
                    flist.write(f"file '{escaped}'\n")
                flist_path = flist.name

            result = subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", flist_path, "-c", "copy", str(output_path)],
                capture_output=True, timeout=120
            )

            if result.returncode == 0:
                size = output_path.stat().st_size
                with db() as conn:
                    conn.cursor().execute(
                        """UPDATE merge_jobs SET status='done', file_path=%s,
                           call_count=%s, completed_at=now() WHERE id=%s""",
                        (str(output_path), len(valid_calls), job_id)
                    )
                log.info("Incident merge job %d done: %s (%d bytes)", job_id, output_path, size)
            else:
                with db() as conn:
                    conn.cursor().execute(
                        "UPDATE merge_jobs SET status='failed', error=%s WHERE id=%s",
                        (result.stderr.decode()[-500:], job_id)
                    )
        except Exception as exc:
            log.error("Incident merge job %d failed: %s", job_id, exc)
            try:
                with db() as conn:
                    conn.cursor().execute(
                        "UPDATE merge_jobs SET status='failed', error=%s WHERE id=%s",
                        (str(exc), job_id)
                    )
            except Exception:
                pass

    threading.Thread(target=_run_incident_merge, args=(job_id, valid_calls, label),
                     daemon=True).start()

    return jsonify({
        "job_id":      job_id,
        "label":       label,
        "track_count": len(valid_calls),
        "status_url":  f"/api/merge/{job_id}",
        "audio_url":   f"/api/merge/{job_id}/audio",
    }), 202
