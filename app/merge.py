"""
Merge jobs — concatenate multiple calls into a single MP3 via ffmpeg.

  POST /api/merge            — create a merge job
  GET  /api/merge            — list merge jobs
  GET  /api/merge/<id>       — job detail
  GET  /api/merge/<id>/audio — stream merged MP3
"""

import logging
import subprocess
import tempfile
import threading
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, abort

from app.config import MERGE_ROOT
from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("merge", __name__)


# -----------------------------------------------------------------------
# Background merge runner
# -----------------------------------------------------------------------

def run_merge(job_id: int):
    """
    Pull calls for the merge job's tg + time window, build an ffmpeg concat
    list, run ffmpeg, update the job status to 'done' or 'failed'.
    """
    log.info("Starting merge job %d", job_id)

    # Mark as running
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE merge_jobs SET status = 'running' WHERE id = %s RETURNING *",
                (job_id,),
            )
            job = cur.fetchone()
            if not job:
                log.error("Merge job %d not found", job_id)
                return
    except Exception as exc:
        log.error("Failed to fetch merge job %d: %s", job_id, exc)
        return

    tg           = job["tg"]
    window_start = job["window_start"]
    window_end   = job["window_end"]
    label        = job["label"] or str(job_id)

    # Sanitise label for use in filename
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)

    # Ensure merge output dir exists
    MERGE_ROOT.mkdir(parents=True, exist_ok=True)
    output_path = MERGE_ROOT / f"{job_id}_{safe_label}.mp3"

    # Query calls
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, file_path
                FROM calls
                WHERE tg = %s
                  AND ts >= %s
                  AND ts <= %s
                ORDER BY ts ASC
                """,
                (tg, window_start, window_end),
            )
            calls = cur.fetchall()
    except Exception as exc:
        _fail_job(job_id, f"DB query failed: {exc}")
        return

    if not calls:
        _fail_job(job_id, "No calls found in time window")
        return

    call_count = len(calls)
    log.info("Merge job %d: found %d calls for tg=%d", job_id, call_count, tg)

    # Check all files exist
    missing = [c["file_path"] for c in calls if not Path(c["file_path"]).exists()]
    if missing:
        log.warning("Merge job %d: %d files missing from disk", job_id, len(missing))

    valid_calls = [c for c in calls if Path(c["file_path"]).exists()]
    if not valid_calls:
        _fail_job(job_id, "No audio files found on disk")
        return

    # Write ffmpeg concat file list
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix=f"merge_{job_id}_"
        ) as flist:
            for c in valid_calls:
                # ffmpeg concat format: escape single quotes in paths
                escaped = str(c["file_path"]).replace("'", "'\\''")
                flist.write(f"file '{escaped}'\n")
            flist_path = flist.name
    except Exception as exc:
        _fail_job(job_id, f"Failed to write concat list: {exc}")
        return

    # Run ffmpeg
    cmd = [
        "ffmpeg",
        "-y",                  # overwrite output if exists
        "-f", "concat",
        "-safe", "0",
        "-i", flist_path,
        "-c", "copy",
        str(output_path),
    ]

    log.info("Merge job %d: running ffmpeg: %s", job_id, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5-minute hard limit
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-500:])  # last 500 chars of stderr
    except Exception as exc:
        log.error("Merge job %d ffmpeg failed: %s", job_id, exc)
        _fail_job(job_id, str(exc))
        return
    finally:
        # Clean up temp file
        try:
            Path(flist_path).unlink(missing_ok=True)
        except Exception:
            pass

    # Mark done
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE merge_jobs
                SET status       = 'done',
                    file_path    = %s,
                    call_count   = %s,
                    completed_at = now()
                WHERE id = %s
                """,
                (str(output_path), call_count, job_id),
            )
        log.info("Merge job %d done: %s (%d calls)", job_id, output_path, call_count)
    except Exception as exc:
        log.error("Merge job %d: failed to update status: %s", job_id, exc)


def _fail_job(job_id: int, error: str):
    """Mark a merge job as failed."""
    log.error("Merge job %d failed: %s", job_id, error)
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE merge_jobs
                SET status = 'failed', error = %s, completed_at = now()
                WHERE id = %s
                """,
                (error[:1000], job_id),
            )
    except Exception as exc:
        log.error("Could not mark job %d as failed: %s", job_id, exc)


# -----------------------------------------------------------------------
# Blueprint routes
# -----------------------------------------------------------------------

@bp.route("/api/merge", methods=["POST"])
def create_merge_job():
    body = request.get_json(silent=True) or {}

    tg           = body.get("tg")
    window_start = body.get("window_start")
    window_end   = body.get("window_end")
    label        = body.get("label", "merge")

    if not all([tg, window_start, window_end]):
        return jsonify({"error": "tg, window_start, window_end are required"}), 400

    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO merge_jobs (tg, window_start, window_end, label, status)
                VALUES (%s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (tg, window_start, window_end, label),
            )
            job_id = cur.fetchone()["id"]
    except Exception as exc:
        log.error("Failed to create merge job: %s", exc)
        return jsonify({"error": "db error"}), 500

    # Kick off in background thread
    t = threading.Thread(target=run_merge, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"status": "ok", "id": job_id}), 200


@bp.route("/api/merge", methods=["GET"])
def list_merge_jobs():
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, tg, label, status, call_count, file_path,
                   created_at, completed_at, error,
                   window_start, window_end
            FROM merge_jobs
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        rows = [dict(r) for r in cur.fetchall()]

    return jsonify({"jobs": rows, "count": len(rows)})


@bp.route("/api/merge/<int:job_id>", methods=["GET"])
def get_merge_job(job_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM merge_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        if not row:
            abort(404)

    return jsonify(dict(row))


@bp.route("/api/merge/<int:job_id>/audio", methods=["GET"])
def stream_merge_audio(job_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT file_path, status FROM merge_jobs WHERE id = %s", (job_id,)
        )
        row = cur.fetchone()
        if not row:
            abort(404)

    if row["status"] != "done":
        return jsonify({"error": f"job status is '{row['status']}', not done"}), 409

    if not row["file_path"]:
        abort(404)

    path = Path(row["file_path"])
    if not path.exists():
        log.error("Merged audio file missing: %s", path)
        abort(404)

    return send_file(str(path), mimetype="audio/mpeg", as_attachment=False)
