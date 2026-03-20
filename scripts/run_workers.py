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
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── PID lockfile — prevents multiple worker instances ────────────────────
_PIDFILE = Path("/tmp/sdrtrunk-workers.pid")

def _check_pidfile():
    if _PIDFILE.exists():
        try:
            old_pid = int(_PIDFILE.read_text().strip())
            # Check if that process is actually running
            os.kill(old_pid, 0)
            print(f"ERROR: Workers already running as PID {old_pid}. Exiting.", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            # PID file stale — process is gone, safe to proceed
            _PIDFILE.unlink(missing_ok=True)
    _PIDFILE.write_text(str(os.getpid()))

def _remove_pidfile():
    _PIDFILE.unlink(missing_ok=True)

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
    """Run the alert check and incident closer every 60 seconds."""
    from app.alerts import run_alert_check
    from app.incidents import close_stale_incidents

    def loop():
        log.info("Alert worker started (60s interval).")
        while not _shutdown.is_set():
            try:
                run_alert_check()
            except Exception as exc:
                log.error("Alert check error: %s", exc)
            try:
                close_stale_incidents()
            except Exception as exc:
                log.error("Incident closer error: %s", exc)
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
    _remove_pidfile()
    _shutdown.set()


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    _check_pidfile()
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    import atexit
    atexit.register(_remove_pidfile)

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
