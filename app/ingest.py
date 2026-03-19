"""
POST /api/call — SDRTrunk Broadcastify-compatible ingest endpoint.

SDRTrunk sends a multipart/form-data POST with:
  apiKey, systemId, callDuration, ts, tg, src, freq, enc, + MP3 binary

Test posts (from SDRTrunk startup) include a "test" field — return plain "OK".
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify

from app.config import API_KEY, ARCHIVE_ROOT
from app.db import db

log = logging.getLogger(__name__)
bp = Blueprint("ingest", __name__)


def _validate_key(key: str) -> bool:
    return key == API_KEY


def _build_path(ts: datetime, tg: int, radio_id: str) -> Path:
    """Build archive path: {ARCHIVE_ROOT}/{YYYYMMDD}/{tg}/{ts_epoch}_{tg}_{radio_id}.mp3"""
    date_str = ts.strftime("%Y%m%d")
    ts_epoch = int(ts.timestamp())
    filename = f"{ts_epoch}_{tg}_{radio_id}.mp3"
    path = ARCHIVE_ROOT / date_str / str(tg) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@bp.route("/api/call", methods=["POST"])
def receive_call():
    # --- Auth ---
    api_key = request.form.get("apiKey", "")
    if not _validate_key(api_key):
        log.warning("Invalid API key from %s", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401

    # --- Test ping from SDRTrunk on startup ---
    if request.form.get("test"):
        log.info("SDRTrunk test ping received from %s", request.remote_addr)
        return "OK 0 calls pending", 200

    # --- Parse form fields ---
    try:
        tg        = int(request.form.get("tg", 0))
        system_id = request.form.get("systemId", "")
        radio_id  = str(request.form.get("src", "0"))
        ts_unix   = int(request.form.get("ts", 0))
        duration  = float(request.form.get("callDuration", 0))
        freq_hz   = int(request.form.get("freq", 0))
    except (ValueError, TypeError) as exc:
        log.error("Bad form fields: %s", exc)
        return jsonify({"error": "bad request", "detail": str(exc)}), 400

    if tg == 0:
        return jsonify({"error": "missing talkgroup"}), 400

    if ts_unix == 0:
        # Fall back to server time
        ts_unix = int(datetime.now(tz=timezone.utc).timestamp())

    # --- Locate the MP3 attachment ---
    audio_file = (
        request.files.get("audio")
        or request.files.get("call")
        or (list(request.files.values())[0] if request.files else None)
    )
    if audio_file is None:
        log.error("No audio file in POST for tg=%d", tg)
        return jsonify({"error": "no audio"}), 400

    # --- Save to disk ---
    ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
    file_path = _build_path(ts, tg, radio_id)

    try:
        audio_file.save(str(file_path))
        file_size = file_path.stat().st_size
        log.info("Saved call tg=%d ts=%s size=%d path=%s", tg, ts.isoformat(), file_size, file_path)
    except Exception as exc:
        log.error("Failed to save MP3: %s", exc)
        return jsonify({"error": "storage error"}), 500

    # --- Insert into DB and NOTIFY workers ---
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO calls (tg, system_id, radio_id, ts, duration_sec, freq_hz, file_path)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tg, system_id, radio_id, ts, duration, freq_hz, str(file_path)),
            )
            row = cur.fetchone()
            call_id = row["id"]

            # Signal transcription worker
            cur.execute("SELECT pg_notify('new_call', %s)", (str(call_id),))

    except Exception as exc:
        log.error("DB insert/notify failed for tg=%d: %s", tg, exc)
        return jsonify({"error": "db error"}), 500

    log.info("Inserted call id=%d tg=%d duration=%.1fs", call_id, tg, duration)
    return jsonify({"status": "ok", "id": call_id}), 200
