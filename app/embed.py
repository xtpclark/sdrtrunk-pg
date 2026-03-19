"""
Embedding + entity extraction worker.

Listens on PostgreSQL 'transcribed_call' NOTIFY channel.
For each notification:
  1. Generates an OpenAI text embedding and stores it in calls.embedding
  2. Runs an LLM prompt to extract addresses, units, and codes from the
     transcript and stores them in call_entities.

Run standalone:
    python -m app.embed
"""

import json
import logging
import select

import psycopg2
import psycopg2.extensions

from app.config import DATABASE_URL, OPENAI_API_KEY, EMBEDDING_MODEL
from app.db import db

log = logging.getLogger(__name__)


def _openai_client():
    """Return an OpenAI client, lazily importing and configuring it."""
    import openai  # lazy import

    openai.api_key = OPENAI_API_KEY
    return openai


# -----------------------------------------------------------------------
# Embedding
# -----------------------------------------------------------------------

def embed_call(call_id: int):
    """
    Fetch the transcript for call_id, generate an embedding via OpenAI,
    and UPDATE calls.embedding.
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — skipping embed for call %d", call_id)
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT transcript FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()

    if not row or not row["transcript"]:
        log.info("embed_call: call %d has no transcript, skipping", call_id)
        return

    transcript = row["transcript"].strip()

    try:
        openai = _openai_client()
        response = openai.embeddings.create(
            model=EMBEDDING_MODEL,
            input=transcript,
        )
        embedding = response.data[0].embedding  # list of floats
    except Exception as exc:
        log.error("OpenAI embedding failed for call %d: %s", call_id, exc)
        return

    # pgvector expects a string like '[0.1, 0.2, ...]'
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"

    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE calls
            SET embedding   = %s::vector,
                embedded_at = now()
            WHERE id = %s
            """,
            (vec_str, call_id),
        )

    log.info("Embedded call %d (%d dims)", call_id, len(embedding))


# -----------------------------------------------------------------------
# Entity extraction
# -----------------------------------------------------------------------

ENTITY_PROMPT = """\
You are a dispatcher radio transcript analyzer. Extract entities from the transcript below.

Return ONLY a JSON array. Each item must have:
  - "entity_type": one of "address", "unit", "code", "location"
  - "value": the extracted text exactly as spoken
  - "confidence": float 0.0–1.0

Rules:
- "address": street addresses, intersections, cross-streets (e.g. "100 Main Street", "Main and Elm")
- "unit": radio unit IDs, badge numbers, apparatus IDs (e.g. "Unit 4", "Engine 7", "Badge 1234")
- "code": police/fire/EMS codes (e.g. "10-4", "Signal 31", "Code 3", "Structure fire")
- "location": named locations without a street address (e.g. "Norfolk Airport", "ODU campus")

Return [] if nothing relevant is found. No explanation, just the JSON array.

Transcript:
{transcript}
"""


def extract_entities(call_id: int):
    """
    Run LLM entity extraction on the transcript and INSERT into call_entities.
    """
    if not OPENAI_API_KEY:
        log.warning("OPENAI_API_KEY not set — skipping entity extraction for call %d", call_id)
        return

    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT transcript FROM calls WHERE id = %s", (call_id,))
        row = cur.fetchone()

    if not row or not row["transcript"]:
        log.info("extract_entities: call %d has no transcript, skipping", call_id)
        return

    transcript = row["transcript"].strip()
    if len(transcript) < 5:
        return

    prompt = ENTITY_PROMPT.format(transcript=transcript)

    try:
        openai = _openai_client()
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as exc:
        log.error("OpenAI chat failed for call %d: %s", call_id, exc)
        return

    # Parse JSON response
    try:
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        entities = json.loads(raw)
        if not isinstance(entities, list):
            raise ValueError(f"Expected list, got {type(entities)}")
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("Failed to parse entity JSON for call %d: %s — raw: %r", call_id, exc, raw[:200])
        return

    if not entities:
        log.info("extract_entities: no entities found for call %d", call_id)
        return

    # Insert into call_entities
    with db() as conn:
        cur = conn.cursor()
        for ent in entities:
            entity_type = ent.get("entity_type", "unknown")
            value       = ent.get("value", "")
            confidence  = ent.get("confidence", 0.0)

            if not value:
                continue

            cur.execute(
                """
                INSERT INTO call_entities (call_id, entity_type, value, confidence)
                VALUES (%s, %s, %s, %s)
                """,
                (call_id, entity_type, value, confidence),
            )

    log.info("Extracted %d entities for call %d", len(entities), call_id)

    # Trigger geocoding for address entities (deferred to geocode module)
    from app.geocode import geocode_call_entities
    try:
        geocode_call_entities(call_id)
    except Exception as exc:
        log.error("Geocoding failed for call %d: %s", call_id, exc)


# -----------------------------------------------------------------------
# LISTEN / NOTIFY worker
# -----------------------------------------------------------------------

def listen_for_transcriptions():
    """
    LISTEN on 'transcribed_call' and run embed + extract for each call.
    """
    log.info("Embedding worker starting. Connecting to DB…")

    conn = psycopg2.connect(DATABASE_URL)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)

    cur = conn.cursor()
    cur.execute("LISTEN transcribed_call;")
    log.info("Listening on 'transcribed_call'…")

    try:
        while True:
            if select.select([conn], [], [], 5.0)[0]:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    call_id_str = notify.payload.strip()
                    if not call_id_str.isdigit():
                        log.warning("Unexpected notify payload: %r", call_id_str)
                        continue
                    call_id = int(call_id_str)
                    log.info("Received transcribed_call notify: call_id=%d", call_id)
                    try:
                        embed_call(call_id)
                    except Exception as exc:
                        log.error("embed_call failed for %d: %s", call_id, exc)
                    try:
                        extract_entities(call_id)
                    except Exception as exc:
                        log.error("extract_entities failed for %d: %s", call_id, exc)
    except KeyboardInterrupt:
        log.info("Embedding worker interrupted.")
    finally:
        cur.execute("UNLISTEN transcribed_call;")
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    listen_for_transcriptions()
