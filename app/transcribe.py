"""
Whisper transcription worker.

Listens on the PostgreSQL 'new_call' NOTIFY channel.
For each notification, transcribes the audio file and updates the calls table.
After transcription, sends 'transcribed_call' notification for the embed worker.

Run standalone:
    python -m app.transcribe
"""

import logging
import select
import time

import psycopg2
import psycopg2.extensions

from app.config import DATABASE_URL, WHISPER_MODEL
from app.db import db

log = logging.getLogger(__name__)

_whisper_model = None


def load_model():
    """Load (and cache) the Whisper model. Safe to call multiple times."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    import whisper  # lazy import — heavy dependency

    log.info("Loading Whisper model '%s'…", WHISPER_MODEL)
    _whisper_model = whisper.load_model(WHISPER_MODEL)
    log.info("Whisper model loaded.")
    return _whisper_model


def transcribe_call(call_id: int):
    """
    Load the audio file for call_id, transcribe with Whisper, and update the
    calls table with the result.

    Raises on DB errors; logs and swallows transcription errors so the worker
    keeps running.
    """
    # Fetch file path
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT file_path FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()

    if not row:
        log.warning("transcribe_call: call %d not found", call_id)
        return

    file_path = row["file_path"]
    log.info("Transcribing call %d: %s", call_id, file_path)

    # Transcribe — on failure, persist empty transcript so the pipeline
    # continues (embed + incident threading still fire via NOTIFY)
    transcript = ""
    try:
        model = load_model()
        result = model.transcribe(
            file_path,
            language="en",                   # skip language detection
            condition_on_previous_text=False, # prevent hallucination chaining
            no_speech_threshold=0.6,          # discard near-silence
            logprob_threshold=-1.0,           # discard low-confidence segments
            compression_ratio_threshold=2.4,  # discard repetitive/noise output
            fp16=True,                        # GPU inference
        )
        # Filter out segments with low avg log probability
        segments = result.get("segments", [])
        if segments:
            avg_logprob = sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
            if avg_logprob < -1.0:
                log.info("Call %d: low confidence (avg_logprob=%.2f), discarding", call_id, avg_logprob)
            else:
                transcript = result.get("text", "").strip()
        else:
            transcript = result.get("text", "").strip()

        # Discard very short transcripts — likely noise artifacts
        if len(transcript) < 3:
            transcript = ""

    except Exception as exc:
        log.error("Whisper failed for call %d (%s): %s — persisting empty transcript", call_id, file_path, exc)
        # transcript stays "" — fall through so NOTIFY is still sent

    log.info("Call %d transcript (%d chars): %s…", call_id, len(transcript), transcript[:80])

    # Persist
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE calls
            SET transcript       = %s,
                transcript_model = %s,
                transcribed_at   = now()
            WHERE id = %s
            """,
            (transcript, WHISPER_MODEL, call_id),
        )

        # Signal embedding worker
        cur.execute("SELECT pg_notify('transcribed_call', %s)", (str(call_id),))

    log.info("Call %d transcription saved, 'transcribed_call' notified.", call_id)


def listen_for_calls():
    """
    Open a dedicated psycopg2 connection and LISTEN on 'new_call'.
    Blocks until interrupted; transcribes each incoming call_id.
    """
    log.info("Transcription worker starting. Connecting to DB…")

    # Pre-load model so first call isn't slow
    load_model()

    conn = psycopg2.connect(DATABASE_URL)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    cur = conn.cursor()
    cur.execute("LISTEN new_call;")
    log.info("Listening on 'new_call'…")

    try:
        while True:
            # select.select blocks until data available or timeout (5 s)
            if select.select([conn], [], [], 5.0)[0]:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    call_id_str = notify.payload.strip()
                    if not call_id_str.isdigit():
                        log.warning("Unexpected notify payload: %r", call_id_str)
                        continue
                    call_id = int(call_id_str)
                    log.info("Received new_call notify: call_id=%d", call_id)
                    try:
                        transcribe_call(call_id)
                    except Exception as exc:
                        log.error("Error transcribing call %d: %s", call_id, exc)
    except KeyboardInterrupt:
        log.info("Transcription worker interrupted.")
    finally:
        cur.execute("UNLISTEN new_call;")
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    listen_for_calls()
