# sdrtrunk-pg

A self-hosted backend for SDRTrunk scanner radio recordings. It ingests MP3 call audio from SDRTrunk's Broadcastify Calls plugin, stores metadata in PostgreSQL, and provides automatic transcription (Whisper), semantic embeddings (OpenAI), entity extraction (addresses, unit IDs, codes), geocoding (Nominatim), and a live Leaflet map for the Hampton Roads area. Designed to run on a home server alongside SDRTrunk.

---

## Prerequisites

| Dependency | Version | Notes |
|---|---|---|
| PostgreSQL | 15+ | |
| [pgvector](https://github.com/pgvector/pgvector) | 0.5+ | Semantic search |
| [PostGIS](https://postgis.net/) | 3.x | Geocoded entity map |
| Python | 3.11+ | |
| ffmpeg | any | Call merging |
| [openai-whisper](https://github.com/openai/whisper) | latest | Transcription (requires torch) |

---

## Setup

### 1. Create the database

```bash
createdb sdrtrunk
psql sdrtrunk -f schema/create.sql
psql sdrtrunk -f schema/seed_alert_rules.sql
```

### 2. Configure environment

```bash
cp .env.example .env
$EDITOR .env
```

Key settings:

- `DATABASE_URL` — PostgreSQL connection string
- `ARCHIVE_ROOT` — where MP3s are saved (must be writable by the app)
- `API_KEY` — must match what you enter in SDRTrunk (see below)
- `OPENAI_API_KEY` — optional; enables embeddings + entity extraction
- `WHISPER_MODEL` — `base` is recommended for scanner audio

### 3. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **Note:** `openai-whisper` requires PyTorch. If you're on CPU-only, install the CPU build of torch first:
> ```bash
> pip install torch --index-url https://download.pytorch.org/whl/cpu
> ```

### 4. Create archive directories

```bash
sudo mkdir -p /var/sdrtrunk/archive /var/sdrtrunk/merges
sudo chown $USER /var/sdrtrunk/archive /var/sdrtrunk/merges
```

### 5. Import talkgroups (optional but recommended)

Export your system's talkgroup list from [RadioReference](https://www.radioreference.com/) as CSV, then:

```bash
python scripts/import_talkgroups.py hamptonroads.csv --system-id VA-HR-P25
```

### 6. Run the Flask app

```bash
python run.py
# or
flask --app "app:create_app" run --port 5010
```

Health check: `curl http://localhost:5010/health`

---

## SDRTrunk Configuration

In SDRTrunk, open **Preferences → Broadcastify Calls** (or your recorder's API settings) and set:

| Field | Value |
|---|---|
| **API URL** | `http://<your-server-ip>:5010/api/call` |
| **API Key** | the value of `API_KEY` in your `.env` |
| **System ID** | your P25 system name (optional) |

SDRTrunk will POST each completed call to `/api/call` as multipart form data.

---

## Running Workers

Workers handle transcription, embeddings, and alerts in the background. They use PostgreSQL `LISTEN/NOTIFY` so there's no polling overhead.

```bash
# Run all workers together
python scripts/run_workers.py

# Or run individually
python -m app.transcribe    # Whisper transcription
python -m app.embed         # OpenAI embeddings + entity extraction
```

Workers are safe to run alongside the Flask app.

---

## API Quick Reference

### Ingest

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/call` | SDRTrunk Broadcastify Calls endpoint |

### Calls

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/calls` | List calls. Query params: `tg`, `category`, `date`, `keyword`, `limit`, `offset` |
| `GET` | `/api/calls/<id>` | Single call detail including extracted entities |
| `GET` | `/api/calls/<id>/audio` | Stream MP3 audio |

### Talkgroups & Stats

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/talkgroups` | List talkgroups with call counts |
| `GET` | `/api/stats` | Call counts by category, busiest TGs, hourly breakdown |

### Merge

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/merge` | Create merge job `{tg, window_start, window_end, label}` |
| `GET` | `/api/merge` | List merge jobs |
| `GET` | `/api/merge/<id>` | Job detail |
| `GET` | `/api/merge/<id>/audio` | Stream merged MP3 |

### Map

| Method | Path | Description |
|---|---|---|
| `GET` | `/map` | Live Leaflet map |
| `GET` | `/map/heatmap` | GeoJSON heatmap data. Query param: `minutes` |
| `GET` | `/map/incidents` | GeoJSON incident pins. Query params: `date`, `tg`, `category` |
| `GET` | `/map/stats` | Dashboard stats JSON |

---

## Phase Roadmap

### Phase 1 — Ingest & Storage ✅
- Broadcastify Calls compatible ingest endpoint
- MP3 archive to disk
- PostgreSQL metadata storage
- Talkgroup directory import

### Phase 2 — Intelligence ✅ (skeleton)
- Whisper transcription via LISTEN/NOTIFY
- OpenAI embeddings for semantic search
- Entity extraction (addresses, units, radio codes)
- Nominatim geocoding with Hampton Roads bias
- Keyword + volume-spike alert engine
- Live Leaflet map with heatmap and incident pins

### Phase 3 — Search & Analysis (planned)
- Vector similarity search across transcripts
- Incident timeline reconstruction
- Daily digest emails
- SDRTrunk talkgroup activity dashboard
- Automatic incident clustering

### Phase 4 — Automation (planned)
- Alert-triggered recording highlights
- Integration with CAD feeds
- Mobile-friendly PWA
