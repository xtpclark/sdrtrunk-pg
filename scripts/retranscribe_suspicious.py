#!/usr/bin/env python3
"""
Re-transcribe calls with suspicious transcripts using tuned Whisper settings.
Also nulls out transcripts for calls where the file is missing.

Usage:
    python scripts/retranscribe_suspicious.py [--dry-run]
"""
import sys, os, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.config import DATABASE_URL, WHISPER_MODEL
from app.db import db

def get_suspicious_ids():
    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, file_path, duration_sec, transcript
            FROM calls
            WHERE transcript IS NOT NULL AND transcript != ''
            AND (
                transcript ILIKE '%fuck%'
                OR transcript ILIKE '%shit%'
                OR transcript ILIKE '%bitch%'
                OR transcript ILIKE '%subscribe%'
                OR (duration_sec < 1.5 AND length(transcript) > 20)
                OR transcript ~ '(\\w+) \\1 \\1'
                OR transcript ILIKE '%thank you%for%calling%'
            )
            ORDER BY id
        """)
        return cur.fetchall()

def retranscribe(rows, dry_run=False):
    import whisper
    print(f"Loading Whisper model '{WHISPER_MODEL}'...")
    model = whisper.load_model(WHISPER_MODEL)
    print(f"Loaded. Re-transcribing {len(rows)} calls...\n")

    fixed = skipped = nulled = 0

    for row in rows:
        call_id   = row["id"]
        file_path = row["file_path"]
        old_tx    = row["transcript"]

        if not Path(file_path).exists():
            print(f"  [{call_id}] FILE MISSING — nulling transcript")
            if not dry_run:
                with db() as conn:
                    conn.cursor().execute(
                        "UPDATE calls SET transcript=NULL, transcribed_at=now() WHERE id=%s",
                        (call_id,))
            nulled += 1
            continue

        try:
            result = model.transcribe(
                file_path,
                language="en",
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                logprob_threshold=-1.0,
                compression_ratio_threshold=2.4,
                fp16=True,
            )
            segments = result.get("segments", [])
            if segments:
                avg_logprob = sum(s.get("avg_logprob", 0) for s in segments) / len(segments)
                if avg_logprob < -1.0:
                    new_tx = ""
                else:
                    new_tx = result.get("text", "").strip()
            else:
                new_tx = result.get("text", "").strip()

            if len(new_tx) < 3:
                new_tx = ""

        except Exception as e:
            print(f"  [{call_id}] ERROR: {e}")
            skipped += 1
            continue

        changed = "→" if new_tx != old_tx else "="
        print(f"  [{call_id}] {changed} OLD: {old_tx[:60]!r}")
        if new_tx != old_tx:
            print(f"       NEW: {new_tx[:60]!r}")

        if not dry_run and new_tx != old_tx:
            with db() as conn:
                conn.cursor().execute(
                    "UPDATE calls SET transcript=%s, transcribed_at=now() WHERE id=%s",
                    (new_tx or None, call_id))
            fixed += 1

    print(f"\nDone: {fixed} fixed, {nulled} nulled (missing file), {skipped} errors")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows = get_suspicious_ids()
    print(f"Found {len(rows)} suspicious transcripts")
    retranscribe(rows, dry_run=args.dry_run)
