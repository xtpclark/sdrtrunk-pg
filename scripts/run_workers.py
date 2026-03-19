#!/usr/bin/env python3
"""
Launch all background workers in separate daemon threads.

Workers:
  - Transcription  (LISTEN on 'new_call')
  - Embedding      (LISTEN on 'transcribed_call')
  - Alert engine   (runs every 60 seconds)

Clean shutdown on SIGINT / SIGTERM.

Usage:
    python scripts/run_workers.py
"""

import logging
import signal
import sys
import threading
import time
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)

log = logging.getLogger("run_workers")

_shutdown = threading.Event()


# -----------------------------------------------------------------------
# Worker starters
# -----------------------------------------------------------------------

def start_transcription_worker():
    """Run the Whisper LISTEN loop in a thread."""
    from app.transcribe import listen_for_calls

    log.info("Starting transcription worker thread.")
    t = threading.Thread(target=listen_for_calls, name="transcription", daemon=True)
    t.start()
    return t


def start_embedding_worker():
    """Run the embed/entity LISTEN loop in a thread."""
    from app.embed import listen_for_transcriptions

    log.info("Starting embedding worker thread.")
    t = threading.Thread(target=listen_for_transcriptions, name="embedding", daemon=True)
    t.start()
    return t


def start_alert_worker():
    """Run the alert check every 60 seconds."""
    from app.alerts import run_alert_check

    def loop():
        log.info("Alert worker started (60s interval).")
        while not _shutdown.is_set():
            try:
                run_alert_check()
            except Exception as exc:
                log.error("Alert check error: %s", exc)
            _shutdown.wait(timeout=60)
        log.info("Alert worker stopped.")

    t = threading.Thread(target=loop, name="alerts", daemon=True)
    t.start()
    return t


# -----------------------------------------------------------------------
# Signal handling
# -----------------------------------------------------------------------

def _handle_signal(signum, frame):
    sig_name = signal.Signals(signum).name
    log.info("Received %s — shutting down…", sig_name)
    _shutdown.set()


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    threads = [
        start_transcription_worker(),
        start_embedding_worker(),
        start_alert_worker(),
    ]

    log.info("All workers started. Press Ctrl+C to stop.")

    # Block until shutdown signal
    _shutdown.wait()

    log.info("Waiting for threads to exit…")
    for t in threads:
        t.join(timeout=10)
        if t.is_alive():
            log.warning("Thread '%s' did not exit cleanly.", t.name)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
