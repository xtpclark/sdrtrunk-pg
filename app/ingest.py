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
        freq_raw  = float(request.form.get("freq", 0))
        # SDRTrunk sends freq in MHz (e.g. 856.6375), convert to Hz
        freq_hz   = int(freq_raw * 1_000_000) if freq_raw < 10_000 else int(freq_raw)
        # GPS coords (optional — only present if radio reported location via P25 LC header)
        lat_raw   = request.form.get("lat")
        lon_raw   = request.form.get("lon")
        lat       = float(lat_raw) if lat_raw else None
        lon       = float(lon_raw) if lon_raw else None
        # Sanity-check coordinates (Hampton Roads bounding box + some margin)
        if lat is not None and not (35.0 <= lat <= 38.5 and -77.5 <= lon <= -74.5):
            log.warning("GPS out of bounds lat=%.5f lon=%.5f — discarding", lat, lon)
            lat = lon = None
    except (ValueError, TypeError) as exc:
        log.error("Bad form fields: %s", exc)
        return jsonify({"error": "bad request", "detail": str(exc)}), 400

    if tg == 0:
        return jsonify({"error": "missing talkgroup"}), 400

    if ts_unix == 0:
        # Fall back to server time
        ts_unix = int(datetime.now(tz=timezone.utc).timestamp())

    # --- Build file path and insert into DB ---
    # Broadcastify uses a two-step protocol:
    # Step 1: POST metadata → we return "0 <upload_url>"
    # Step 2: SDRTrunk PUTs the MP3 to that URL
    ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
    file_path = _build_path(ts, tg, radio_id)

    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO calls (tg, system_id, radio_id, ts, duration_sec, freq_hz, file_path, lat, lon)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tg, system_id, radio_id, ts, duration, freq_hz, str(file_path), lat, lon),
            )
            row = cur.fetchone()
            call_id = row["id"]
    except Exception as exc:
        log.error("DB insert failed for tg=%d: %s", tg, exc)
        return jsonify({"error": "db error"}), 500

    gps_str = f" gps=({lat:.5f},{lon:.5f})" if lat is not None else ""
    log.info("Registered call id=%d tg=%d duration=%.1fs%s — awaiting audio upload", call_id, tg, duration, gps_str)

    # Return upload URL per Broadcastify two-step protocol
    upload_url = f"http://localhost:5010/api/call/upload/{call_id}"
    return f"0 {upload_url}", 200


@bp.route("/api/call/upload/<int:call_id>", methods=["PUT"])
def receive_audio(call_id):
    """Step 2: receive the MP3 PUT from SDRTrunk."""
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT file_path, tg FROM calls WHERE id = %s", (call_id,))
            row = cur.fetchone()
    except Exception as exc:
        log.error("DB lookup failed for call_id=%d: %s", call_id, exc)
        return "ERROR db", 500

    if not row:
        return "ERROR not found", 404

    file_path = Path(row["file_path"])
    tg = row["tg"]
    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        file_path.write_bytes(request.get_data())
        file_size = file_path.stat().st_size
        log.info("Saved audio call_id=%d tg=%d size=%d path=%s", call_id, tg, file_size, file_path)
    except Exception as exc:
        log.error("Failed to save MP3 for call_id=%d: %s", call_id, exc)
        return "ERROR storage", 500

    # Signal transcription worker
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT pg_notify('new_call', %s)", (str(call_id),))
    except Exception as exc:
        log.warning("NOTIFY failed for call_id=%d: %s", call_id, exc)

    return "OK", 200
