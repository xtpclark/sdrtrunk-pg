#!/usr/bin/env python3
"""
Backfill embeddings for all transcribed calls that don't have one yet.
Runs in batches to avoid rate limiting.

Usage:
    python scripts/backfill_embeddings.py [--limit N]
"""
import sys, argparse, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import db
import app.embed as _embed_mod
_embed_mod._gemini_client = None  # force fresh client with current key
from app.embed import get_embedding, EMBEDDING_PROVIDER

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Max calls to process (0=all)")
    args = parser.parse_args()

    with db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, transcript FROM calls
            WHERE transcript IS NOT NULL
              AND transcript != ''
              AND embedding IS NULL
            ORDER BY id
        """ + (f" LIMIT {args.limit}" if args.limit else ""))
        rows = cur.fetchall()

    print(f"Backfilling embeddings for {len(rows)} calls via {EMBEDDING_PROVIDER}...")

    done = errors = 0
    for i, row in enumerate(rows):
        call_id  = row["id"]
        transcript = row["transcript"].strip()

        vec = get_embedding(transcript)
        if vec:
            vec_str = "[" + ",".join(str(v) for v in vec) + "]"
            with db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE calls SET embedding=%s::vector, embedded_at=now() WHERE id=%s",
                    (vec_str, call_id)
                )
            done += 1
        else:
            errors += 1

        if (i+1) % 50 == 0:
            print(f"  {i+1}/{len(rows)} — {done} embedded, {errors} errors")
            time.sleep(0.5)  # brief pause every 50 to avoid rate limits

    print(f"\nDone: {done} embedded, {errors} errors")

if __name__ == "__main__":
    main()
