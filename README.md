# sdrtrunk-pg

A self-hosted scanner radio command center. Ingests P25 call audio from SDRTrunk's Broadcastify Calls plugin, transcribes it with Whisper, extracts entities (addresses, units, codes), geocodes locations against city open data, threads calls into incidents, and presents everything on a live map dashboard with semantic search.

Built on PostgreSQL + PostGIS + pgvector. Runs on a home server alongside SDRTrunk.

**Designed to be city-portable** — swap in a `config.yaml` for your city and it works for Detroit, Portland, or wherever your scanner points.

![Hampton Roads Command Center](docs/screenshot.png)

---

## Features

- **Live call ingest** — Broadcastify Calls two-step protocol, MP3 archive to disk
- **Transcription** — Whisper (tuned for scanner audio), GPU-accelerated
- **Semantic embeddings** — pgvector, Gemini or OpenAI, enables similarity search
- **Entity extraction** — LLM-powered: addresses, unit IDs, radio codes, named locations
- **Geocoding** — local city address DB (78k+ addresses, instant, no rate limits) + Nominatim fallback
- **Incident threading** — groups related calls by geo proximity, radio ID, talkgroup window
- **Command center UI** — live map, incident sidebar, activity chart, semantic search, priority card
- **Alert engine** — keyword and volume-spike rules, webhook delivery

---

## Prerequisites

| Dependency | Notes |
|---|---|
| PostgreSQL 15+ | with pgvector and PostGIS extensions |
| Python 3.11+ | |
| ffmpeg | for call merging |
| openai-whisper | requires PyTorch; GPU strongly recommended |
| SDRTrunk | configured with Broadcastify Calls broadcaster |

---

## Quick Start

### 1. Database

```bash
createdb sdrtrunk
psql sdrtrunk -f schema/create.sql
psql sdrtrunk -f schema/seed_alert_rules.sql
```

### 2. Environment

```bash
cp .env.example .env
$EDITOR .env
```

Required:
```env
DATABASE_URL=postgresql://user:pass@localhost:5432/sdrtrunk
ARCHIVE_ROOT=/path/to/mp3/archive
API_KEY=your-secret-key-here          # must match SDRTrunk config
```

AI providers — pick one (or both):
```env
GEMINI_API_KEY=...                    # recommended; free tier works
OPENAI_API_KEY=...                    # alternative

EMBEDDING_PROVIDER=gemini             # gemini | openai | local
ENTITY_PROVIDER=gemini                # gemini | openai | none
```

City config (defaults to Norfolk, VA):
```env
CITY_CONFIG=data/cities/norfolk-va/config.yaml
```

### 3. Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> **GPU note:** For Whisper GPU support, install torch with CUDA before running pip install:
> ```bash
> pip install torch --index-url https://download.pytorch.org/whl/cu121
> ```

### 4. Import talkgroups

Export your P25 system's talkgroup CSV from [RadioReference](https://www.radioreference.com/), then:

```bash
python scripts/import_talkgroups.py hamptonroads.csv --system-id VA-HR-P25
```

### 5. Load address database

Downloads city address points for fast local geocoding:

```bash
python scripts/load_address_db.py        # primary city only
python scripts/load_address_db.py --all  # primary + adjacent cities
```

### 6. Start the app

```bash
# Flask app (serves the UI and ingest endpoint)
python run.py

# Background workers (transcription, embeddings, alerts) — in a second terminal
python scripts/run_workers.py
```

Map: `http://localhost:5010/map`  
Health: `http://localhost:5010/health`

---

## SDRTrunk Configuration

In SDRTrunk, open your talkgroup aliases and add a **Broadcastify Calls** broadcast channel to each talkgroup you want to capture. Then configure the broadcaster:

| Field | Value |
|---|---|
| API URL | `http://<your-server>:5010/api/call` |
| API Key | value of `API_KEY` in your `.env` |
| System ID | your P25 system name (e.g. `VA-HR-P25`) |

SDRTrunk sends a two-step POST: metadata first, then a PUT with the MP3 binary. Both steps must succeed for a call to be recorded.

---

## Adding a New City

All city-specific configuration lives in `data/cities/{slug}/`:

```
data/cities/
  norfolk-va/
    config.yaml             ← bbox, map center, address DB source URL
    street_corrections.json ← Whisper mis-transcription fixes
    landmarks.json          ← named location hints for entity extraction
```

To add Portland, OR:

```bash
cp -r data/cities/norfolk-va data/cities/portland-or
# Edit config.yaml: name, bbox, map_center, address_db URL + field mappings
# Edit street_corrections.json: local Whisper quirks for your area
# Edit landmarks.json: local named places
echo "CITY_CONFIG=data/cities/portland-or/config.yaml" >> .env
python scripts/load_address_db.py
```

`config.yaml` schema:
```yaml
name: "Portland, OR"
map_center: [45.5051, -122.6750]
map_zoom: 12
bbox: [-123.5, 45.2, -122.2, 45.8]    # [sw_lon, sw_lat, ne_lon, ne_lat]
geocode_context: "Portland, OR"
entity_context: "Portland, Oregon"
address_db:
  source: socrata                        # socrata | arcgis | csv
  url: "https://opendata.portland.gov/resource/..."
  fields:
    full_address: address
    lat: lat
    lon: lon
  city_value: "Portland"
```

No code changes required.

---

## Architecture

```
SDRTrunk
  └── POST /api/call (Broadcastify two-step)
        ├── calls table  ←── metadata + file_path
        └── pg_notify('new_call')
              │
              ▼
        transcribe.py  (Whisper, LISTEN new_call)
              └── pg_notify('transcribed_call')
                    │
                    ▼
              embed.py  (Gemini/OpenAI, LISTEN transcribed_call)
                    ├── embeddings → calls.embedding
                    ├── entities  → call_entities
                    │     └── geocode_call_entities()
                    │           ├── local address_db lookup (pg_trgm)
                    │           └── Nominatim fallback
                    └── process_call_for_incidents()
                          └── incidents + incident_calls tables

Flask app
  ├── /map           → command center UI
  ├── /api/threads   → active incident list
  ├── /api/incidents/{id}/detail
  ├── /api/calls/search?q=...  → pgvector semantic search
  ├── /map/stats     → KPIs, histogram, urgent incident
  └── /map/incidents_geo → GeoJSON for map layer
```

### Incident Threading

Every transcribed call is evaluated against active incidents:

1. **Geo proximity** — same category, within 500m, last 30 min
2. **Radio anchor** — same category, same radio_id in active incident, last 30 min  
3. **TG window** — same talkgroup, activity within ±3 min
4. **New incident** — create if no match found

Race conditions are prevented by a `UNIQUE(anchor_call_id)` DB constraint — concurrent creates resolve to the same incident via `ON CONFLICT`.

---

## API Reference

### Ingest
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/call` | SDRTrunk Broadcastify step 1 — returns `"0 <upload_url>"` |
| `PUT` | `/api/call/upload/<id>` | Broadcastify step 2 — receives MP3 binary |

### Calls
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/calls` | List calls. Params: `tg`, `category`, `date`, `keyword`, `limit`, `offset` |
| `GET` | `/api/calls/<id>` | Single call with extracted entities |
| `GET` | `/api/calls/<id>/audio` | Stream MP3 |
| `GET` | `/api/calls/search` | Semantic search. Params: `q`, `limit`, `category` |

### Incidents
| Method | Path | Description |
|---|---|---|
| `GET` | `/api/threads` | Active incident threads. Params: `minutes` |
| `GET` | `/api/incidents/<id>/detail` | Incident with calls, units, transcript timeline |

### Map
| Method | Path | Description |
|---|---|---|
| `GET` | `/map` | Live command center UI |
| `GET` | `/map/stats` | KPIs, histogram, top TGs, urgent incident |
| `GET` | `/map/heatmap` | GeoJSON call density. Params: `minutes` |
| `GET` | `/map/incidents_geo` | GeoJSON incident pins. Params: `minutes`, `status` |

### Meta
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/api/talkgroups` | Talkgroup list with call counts |
| `GET` | `/api/stats` | Aggregate stats — calls by category, busiest TGs, hourly |

---

## Database Schema

Key tables:

| Table | Description |
|---|---|
| `calls` | Every call — metadata, transcript, embedding, GPS if available |
| `call_entities` | Extracted entities (address, unit, code, location) with lat/lon |
| `incidents` | Threaded incident groups |
| `incident_calls` | Many-to-many call↔incident with join reason |
| `talkgroups` | TG directory — alpha tags, categories, descriptions |
| `address_db` | City address points for local geocoding |
| `alert_rules` | Keyword and volume-spike alert definitions |
| `alerts` | Fired alert log |

---

## Tips & Gotchas

See `NAPKIN.md` for hard-won lessons on Whisper tuning, the Broadcastify two-step protocol, SDRTrunk alias wiring, and geocoding edge cases.

---

## License

MIT
