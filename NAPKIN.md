# NAPKIN.md — sdrtrunk-pg

Lessons learned, gotchas, and things future-me needs to know.

## Architecture

### The Two-Step Protocol
SDRTrunk's Broadcastify broadcaster is NOT a simple POST. It's two steps:
1. POST metadata fields → server returns `"0 <upload_url>"`
2. PUT the MP3 binary to that URL

If you return JSON or anything other than `"0 <url>"` from step 1, SDRTrunk
silently drops the call. Spent time debugging this. The source is in
`BroadcastifyCallBroadcaster.java` lines 296-317.

### Frequency is MHz not Hz
SDRTrunk sends `freq` as MHz float (e.g. `856.6375`), not Hz.
We convert: `int(freq_raw * 1_000_000) if freq_raw < 10_000 else int(freq_raw)`

### Alias → Broadcast Channel wiring
SDRTrunk only streams calls for talkgroups that have a `<broadcastChannel>` 
element in their alias. Without it, calls record locally but never POST to us.
Script: `scripts/import_from_playlist.py` handles bulk wiring from playlist XML.
Do NOT edit the playlist XML while SDRTrunk is actively decoding — it WILL freeze.

## Whisper

### Tuning for scanner audio
Default Whisper settings hallucinate badly on P25 noise. Required settings:
```python
model.transcribe(
    file_path,
    language="en",                   # skip detection on short clips
    condition_on_previous_text=False, # prevents chaining hallucinations
    no_speech_threshold=0.6,
    logprob_threshold=-1.0,
    compression_ratio_threshold=2.4,
    fp16=True,                        # GPU
)
```
Also discard transcripts with avg_logprob < -1.0 — low confidence = noise.
Short calls (<1.5s) are the worst offenders.

### Common hallucinations on P25
- "Beep Beep Beep" — P25 alerting tones (TG 653 NFD Alerting)
- Foreign language (French, Telugu, Cyrillic) — pure noise
- "subscribers" / "thank you for calling" — radio silence artifacts
- Profanity on very short clips — almost always wrong

### GPU acceleration
RTX 4060 on cadws → ~1s per call. Use `fp16=True`. Don't run `fp16=False`
on GPU — it's 3x slower for no benefit.

## Embeddings

### Provider config
`EMBEDDING_PROVIDER` in `.env`: `gemini` | `openai` | `local`
`ENTITY_PROVIDER` in `.env`: `gemini` | `openai` | `none`

- Gemini `gemini-embedding-001` → 1536 dims, free tier, recommended
- `all-MiniLM-L6-v2` (local/sentence-transformers) → 384 dims, offline
- Schema `vector(N)` must match provider dims — mismatch = silent fail

### Gemini key management
Gemini API keys get auto-revoked if they appear in AI context (MEMORY.md, 
TOOLS.md, etc). Keep the key ONLY in:
- `.env` (gitignored)
- `~/.openclaw/workspace/.secrets`
- `~/.openclaw/agents/main/agent/auth-profiles.json` (for OpenClaw memory)

Don't put it in MEMORY.md or any file loaded into AI context.

## Geocoding

### Lookup order
1. **Local address_db** (pg_trgm fuzzy match) — instant, no rate limit, handles apartments and minor Whisper errors
2. **Local intersection lookup** — ST_DWithin join between two street address sets
3. **Nominatim** (rate-limited to 1 req/sec) — fallback for VB/Chesapeake/Portsmouth addresses not in Norfolk DB

### Only geocode 'address' entities
`location` entities (e.g. "the library", "East Little Creek") are too 
ambiguous. Only `entity_type='address'` gets geocoded.

### Always validate bbox
Even with `bounded=1`, validate with `_in_bbox(lat, lon)` before saving.
Bbox comes from city config — don't hardcode coordinates in geocode.py.

### City context helps
Always append `", Norfolk, VA"` (from `CITY_GEOCODE_CONTEXT`) to queries.
"Military Highway" → "Military Highway, Norfolk, VA" disambiguates.

### Street corrections dict
Whisper reliably mishears certain Hampton Roads streets. These live in
`data/cities/norfolk-va/street_corrections.json` — add new ones as discovered.
Longest-match-first, break after first hit to prevent double-replacement.

### Common Whisper geocoding failures (accepted)
- "Church and Johnson" (no city context) — ambiguous intersection, returns None ✓
- "I'm at the scene" — not an address, correctly returns None ✓  
- Very short transcripts with no address content — expected None ✓

## Incident Threading

### Logic order matters (v2)
1. **Geo proximity** — same category, within 500m, last 30 min (most reliable)
2. **Radio anchor** — same category, radio_id in active incident (cross-category disabled)
3. **TG window** — same talkgroup ±3 min (loosest signal)
4. **Create new** — anchored to this call_id

### Race condition fix
The `UNIQUE(anchor_call_id)` constraint on `incidents` prevents duplicate
anchors when the embed worker processes a backlog of calls on the same TG.
`INSERT ... ON CONFLICT DO NOTHING RETURNING id` — if no row returned,
find the winner's incident by anchor_call_id and join it.

### Category scoping is critical
Without it, a police radio that later keys on a City Services TG pulls
Waste Management into a police incident. Scope `anchor_radio` to same
category only. `radio_id='0'` (P25 unknown src) excluded entirely.

### NSO Courts + NSO Jail
These are the same building complex — it's correct for them to thread
together. Don't try to separate them. They'll run for an entire shift (8h+)
so the stale closer is set to 20 min, not 15.

### Singleton explosion
When the embed worker crashes and is restarted, calls that missed the
NOTIFY queue never get threaded. Run the backfill:
```python
# Quick backfill for unthreaded transcribed calls
from app.incidents import process_call_for_incidents
from app.db import db

with db() as conn:
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM calls WHERE transcript IS NOT NULL
          AND id NOT IN (SELECT call_id FROM incident_calls) ORDER BY ts
    """)
    for row in cur.fetchall():
        process_call_for_incidents(row['id'])
```

### Closing
Incidents auto-close after 20 min of no activity (`close_stale_incidents()`).
Runs in the alert worker loop every 60s.

## Archive

- Location: `/home/pclark/sdrtrunk-archive/archive/`
- Structure: `{YYYYMMDD}/{tg}/{ts_epoch}_{tg}_{radio_id}.mp3`
- Rate: ~380MB/day at 16kbps CBR, 600 calls/hour
- DB file_path column must match actual disk location — update both if moving

## SDRTrunk Setup

### Playlist safety
- Edit playlist BEFORE starting decoding, never during
- After editing XML directly (not via UI), restart SDRTrunk
- `scripts/import_from_playlist.py` reads XML → seeds talkgroups table

### Hardware (cadws)
- 2x RTL-SDR.COM v3 + 2x NESDR Smart
- Discone antenna in attic at 1300 Elk Ave 23518 (Ocean View)
- 50' coax run — ~3-4dB loss at 850MHz, acceptable
- Norfolk P25 primary: 857.5125 MHz
- Excellent LOS to Norfolk/NSN due to flat coastal terrain

## Database

- DB: `sdrtrunk` on localhost:5432 (postgres/pgtest)
- Extensions: postgis, pgvector, pgcrypto
- `calls.embedding` dimension must match provider (currently 1536 for Gemini)
- `mv_call_volume_baseline` needs nightly REFRESH for alert baselines

## Workers

Start: `cd /home/pclark/git/sdrtrunk-pg && nohup .venv/bin/python scripts/run_workers.py >> /tmp/sdrtrunk-workers.log 2>&1 &`
Logs: `tail -f /tmp/sdrtrunk-workers.log`
Kill: `kill -9 $(ps aux | grep run_workers | grep -v grep | awk '{print $2}')`

### PID lockfile
Workers write `/tmp/sdrtrunk-workers.pid` on startup and check it on launch.
A second launch attempt will immediately exit with an error message.
If the process died uncleanly, delete the stale pidfile: `rm /tmp/sdrtrunk-workers.pid`

### Zombie workers cause backlog
Multiple worker instances compete for the same NOTIFY queue. Calls end up
processed by the wrong worker and incident threading gets fragmented.
If you see `embed_backlog > 0` in health checks, check `ps aux | grep run_workers` —
kill all but one.

Workers restart needed after: `.env` changes, `app/*.py` changes (Flask
auto-reloads but workers don't).

## SDRTrunk JAR Patching (GPS)

The running SDRTrunk version is v0.6.1 (pre-built at `~/git/sdr-trunk-linux-x86_64-v0.6.1`).
The JAR has been patched to send `lat`/`lon` fields in the Broadcastify POST
when a radio broadcasts a P25 LRRP GPS location packet.

Patch applied: 2026-03-20
Backup: `sdr-trunk-0.6.1.jar.bak`
Source: `~/git/sdrtrunk` (tag v0.6.1, files: FormField.java, BroadcastifyCallBroadcaster.java)
Compiled with: Bellsoft Liberica JDK 25 full (`/tmp/jdk-25.0.1-full/`)

Norfolk P25 does not currently broadcast LRRP — zero GPS packets seen.
If they ever enable it, coordinates will flow automatically without
further code changes.

## Origin

Built 2026-03-19 in ~4 hours on top of the 2023 `sdrtrunk_recording_parser` 
bash POC (xtpclark/sdrtrunk_recording_parser). The POC proved:
- Broadcastify protocol hook
- TG metadata importance  
- ffmpeg stream copy for merges
- PostgreSQL as the right backend

The POC hit a sequence/unique-constraint bug and went dormant. This is v2.
