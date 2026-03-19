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

### Only geocode 'address' entities
`location` entities (e.g. "the library", "East Little Creek") are too 
ambiguous — Nominatim will geocode them to wrong places worldwide.
Only `entity_type='address'` gets geocoded.

### Always validate bbox
Nominatim returns results outside Hampton Roads even with `bounded=1`.
Always validate with `_in_hampton_roads(lat, lon)` before saving.
Hampton Roads bbox: SW(-76.9, 36.5) → NE(-75.9, 37.3)

### City context helps
Always append `", Norfolk, VA"` to address strings before geocoding.
"Military Highway" → "Military Highway, Norfolk, VA" gets the right one.

### Whisper address mishearings
Common patterns that fail geocoding (Whisper errors):
- "East Low Creek" = East Little Creek
- "Villagard Road" = unknown (possibly Villard or Willard)
- "8380 Kview Avenue" = unknown street
These will return None from geocoder — that's correct behavior, don't force pins.

## Incident Threading

### Logic order matters
1. Geo proximity (500m) checked first — most reliable signal
2. Radio ID match — unit already in an active incident
3. TG window (±5 min same talkgroup) — loosest signal
4. Create new incident only if geocoded address present
Radio-only calls (no address) can JOIN incidents but not CREATE them.

### Closing
Incidents auto-close after 15 min of no activity (`close_stale_incidents()`).
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

Workers restart needed after: `.env` changes, `app/*.py` changes (Flask
auto-reloads but workers don't).

## Origin

Built 2026-03-19 in ~4 hours on top of the 2023 `sdrtrunk_recording_parser` 
bash POC (xtpclark/sdrtrunk_recording_parser). The POC proved:
- Broadcastify protocol hook
- TG metadata importance  
- ffmpeg stream copy for merges
- PostgreSQL as the right backend

The POC hit a sequence/unique-constraint bug and went dormant. This is v2.
