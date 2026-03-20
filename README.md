# sdrtrunk-pg

> A self-hosted P25 scanner radio command center that turns a $30 RTL-SDR dongle into a live dispatch intelligence platform.

![Norfolk, VA Command Center — live](docs/screenshot.png)

*Norfolk, VA — 2,747 calls transcribed, 25 active incidents threaded, 25.8 calls/min. The red dot at bottom: "Center to facility with a 72-year-old male patient, witnessed arrest by family members, CPR was in progress upon arrival." That call came in 19 seconds ago.*

---

Hooks into [SDRTrunk](https://github.com/DSheirer/sdrtrunk)'s Broadcastify Calls plugin, receives every P25 call your scanner hears, and runs it through a pipeline:

```
Audio → Whisper transcription → Gemini entity extraction → local geocoding
      → incident threading → live map + semantic search
```

**What you get:**
- Every call transcribed in ~1 second (GPU) or ~5 seconds (CPU)
- Addresses extracted from transcripts, geocoded against 78k+ city address points
- Related calls automatically grouped into incidents by location, unit, and talkgroup
- A dark-mode command center UI that looks like something out of a real EOC
- Semantic search: "show me all calls about a structure fire" returns ranked results
- Priority incident card that surfaces the most active situation right now

**Works for any city.** Drop in a `config.yaml` for your metro area and it runs for Detroit, Portland, Charlotte, or wherever your scanner points.

---

## What it looks like in practice

**Priority card** — the highest-urgency active incident pinned to the top of the sidebar, scored by category (Fire/EMS gets a 5x multiplier), calls in the last 5 minutes, and unit count. When a fire gets rolling, this goes red and pulses.

**Incident threading** — calls are grouped into incidents using a cascade: geo proximity → same radio ID → same talkgroup within 3 minutes. A race-condition-safe DB constraint ensures two simultaneous calls on the same TG never create duplicate incidents.

**Local geocoding** — addresses are matched against your city's open data address database using `pg_trgm` fuzzy matching. "827 Norview Ave Apt 311" hits instantly with no rate limiting. "Church Street and Johnson Avenue" resolves to the intersection centroid. Nominatim is the fallback for addresses outside the primary city.

**Semantic search** — type "medical emergency" in the search bar and get the 20 most semantically similar calls from today, ranked by embedding cosine similarity. Powered by pgvector and Gemini embeddings.

**Category filter** — toggle Police / Fire / EMS / Military / City Services / Government / Other. The map, sidebar, and live feed all update simultaneously.

---

## Requirements

| | |
|---|---|
| **PostgreSQL 15+** | with `pgvector` and `PostGIS` extensions |
| **Python 3.11+** | |
| **SDRTrunk** | with Broadcastify Calls broadcaster configured |
| **RTL-SDR** (or similar) | one dongle per simultaneous traffic channel you want to decode |
| **Gemini API key** | free tier works fine; ~1000 calls/day at zero cost |
| **GPU** (optional but recommended) | Whisper `small` model at 1s/call vs 5s/call on CPU |

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
```

```env
# Required
DATABASE_URL=postgresql://user:pass@localhost:5432/sdrtrunk
ARCHIVE_ROOT=/path/to/mp3/archive       # needs ~400MB/day of space
API_KEY=pick-something-secret            # must match SDRTrunk config

# AI — get a free Gemini key at aistudio.google.com
GEMINI_API_KEY=your-key-here
EMBEDDING_PROVIDER=gemini
ENTITY_PROVIDER=gemini

# City (defaults to Norfolk, VA — see "Adding a City" below)
CITY_CONFIG=data/cities/norfolk-va/config.yaml

# Whisper
WHISPER_MODEL=small                      # base | small | medium | large
```

### 3. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

> **GPU (CUDA):** Install torch first:
> ```bash
> pip install torch --index-url https://download.pytorch.org/whl/cu121
> ```

### 4. Import talkgroups

Download your P25 system's talkgroup CSV from [RadioReference](https://www.radioreference.com/):

```bash
python scripts/import_talkgroups.py hamptonroads.csv --system-id VA-HR-P25
```

### 5. Load the address database

Downloads city address points for fast local geocoding:

```bash
python scripts/load_address_db.py        # Norfolk only
python scripts/load_address_db.py --all  # Norfolk + Virginia Beach + adjacent cities
```

### 6. Start everything

```bash
# Terminal 1 — Flask app (ingest + UI + API)
python run.py

# Terminal 2 — Background workers (transcription, embeddings, alerts)
python scripts/run_workers.py
```

Open `http://localhost:5010/map` and watch the calls roll in.

---

## SDRTrunk Configuration

In SDRTrunk, open your alias list and add a **Broadcastify Calls** broadcast channel to each talkgroup you want to capture. Then configure the broadcaster:

| Field | Value |
|---|---|
| API URL | `http://your-server:5010/api/call` |
| API Key | the `API_KEY` from your `.env` |
| System ID | your P25 system name (e.g. `VA-HR-P25`) |

SDRTrunk sends a two-step POST: metadata first (returns `"0 <upload_url>"`), then the MP3 binary as a PUT. See `NAPKIN.md` for the full protocol details.

---

## Adding a New City

Everything city-specific lives in `data/cities/{slug}/`:

```
data/cities/
  norfolk-va/
    config.yaml              ← bbox, map center, address DB source
    street_corrections.json  ← Whisper mis-transcription fixes
    landmarks.json           ← named location hints for entity extraction
  detroit-mi/
    config.yaml              ← your city goes here
    ...
```

Create a new city:

```bash
cp -r data/cities/norfolk-va data/cities/portland-or
$EDITOR data/cities/portland-or/config.yaml
echo "CITY_CONFIG=data/cities/portland-or/config.yaml" >> .env
python scripts/load_address_db.py
```

`config.yaml` for Portland:

```yaml
name: "Portland, OR"
map_center: [45.5051, -122.6750]
map_zoom: 12
bbox: [-123.5, 45.2, -122.2, 45.8]   # [sw_lon, sw_lat, ne_lon, ne_lat]
geocode_context: "Portland, OR"
entity_context: "Portland, Oregon"
address_db:
  source: socrata
  url: "https://opendata.portland.gov/resource/YOUR-DATASET-ID.csv"
  fields:
    full_address: full_address
    lat: lat
    lon: lon
  city_value: "Portland"
```

Supported `source` types: `socrata`, `arcgis`, `csv`. No code changes required.

---

## How It Works

### The pipeline

```
SDRTrunk
  └─ POST /api/call  (Broadcastify two-step protocol)
       ├─ Saves MP3 to ARCHIVE_ROOT/{date}/{tg}/{epoch}_{tg}_{radio}.mp3
       ├─ Inserts row into calls table
       └─ pg_notify('new_call', call_id)

transcribe.py  (LISTEN new_call)
  └─ Whisper → calls.transcript
     └─ pg_notify('transcribed_call', call_id)

embed.py  (LISTEN transcribed_call)
  ├─ Gemini embedding → calls.embedding (pgvector, 1536 dims)
  ├─ Gemini entity extraction → call_entities
  │    └─ geocode_call_entities()
  │         ├─ pg_trgm fuzzy match → address_db  (instant, no rate limit)
  │         ├─ Intersection centroid query        (ST_DWithin join)
  │         └─ Nominatim fallback                 (1 req/sec)
  └─ process_call_for_incidents()
       ├─ Geo proximity match  (same category, 500m, last 30min)
       ├─ Radio ID match       (same category, unit in active incident)
       ├─ TG window match      (same TG, ±3 minutes)
       └─ Create new incident  (UNIQUE anchor_call_id prevents race dupes)
```

### Incident threading

Every call is evaluated against open incidents in priority order:

1. **Geo proximity** — active incident of the same category within 500m? Join it.
2. **Radio anchor** — this radio already in an active incident of the same category? Join it. (Cross-category disabled — prevents a police radio keying on a Waste Management channel from merging your city services incident into a police incident.)
3. **TG window** — activity on this exact talkgroup within the last 3 minutes? Join it.
4. **New incident** — nothing matched. Create one.

A `UNIQUE(anchor_call_id)` constraint on the incidents table prevents the race condition where two calls on the same TG arrive simultaneously and both try to create a new incident. The loser of the race finds the winner's incident and joins it instead.

Incidents auto-close after 20 minutes of silence.

---

## API Reference

### Ingest
| Method | Path | |
|---|---|---|
| `POST` | `/api/call` | SDRTrunk Broadcastify step 1 — returns `"0 <upload_url>"` |
| `PUT` | `/api/call/upload/<id>` | Step 2 — receives MP3 binary, triggers transcription |

### Calls
| Method | Path | |
|---|---|---|
| `GET` | `/api/calls` | List calls. Params: `tg`, `category`, `date`, `keyword`, `limit`, `offset` |
| `GET` | `/api/calls/<id>` | Single call with extracted entities |
| `GET` | `/api/calls/<id>/audio` | Stream MP3 |
| `GET` | `/api/calls/search?q=...` | Semantic search (pgvector cosine similarity). Params: `q`, `limit`, `category` |

### Incidents
| Method | Path | |
|---|---|---|
| `GET` | `/api/threads` | Active incident threads. Params: `minutes` (default 120) |
| `GET` | `/api/incidents/<id>/detail` | Incident with call timeline, units, transcripts |

### Map & Stats
| Method | Path | |
|---|---|---|
| `GET` | `/map` | Live command center UI |
| `GET` | `/map/stats` | KPIs, activity histogram, top talkgroups, urgent incident |
| `GET` | `/map/heatmap` | GeoJSON call density. Params: `minutes` |
| `GET` | `/map/incidents_geo` | GeoJSON incident pins. Params: `minutes`, `status` |

### Meta
| Method | Path | |
|---|---|---|
| `GET` | `/health` | `{"status": "ok"}` |
| `GET` | `/api/talkgroups` | Talkgroup directory with call counts |
| `GET` | `/api/stats` | Aggregate stats — calls by category, busiest TGs, hourly breakdown |

---

## Database Schema

| Table | Description |
|---|---|
| `calls` | Every call — metadata, file path, transcript, embedding, GPS coords if available |
| `call_entities` | LLM-extracted entities with geocoded lat/lon |
| `incidents` | Threaded incident groups |
| `incident_calls` | Call ↔ incident mapping with join reason |
| `talkgroups` | TG directory — alpha tags, categories, descriptions |
| `address_db` | City address points (78k+ for Norfolk; add more cities freely) |
| `alert_rules` | Keyword and volume-spike rule definitions |
| `alerts` | Fired alert log |

---

## GPS / LRRP

P25 systems *can* broadcast unit GPS coordinates via LRRP (Location Registration and Reporting Protocol). When a municipality enables this, Motorola and L3Harris radios transmit their GPS position as part of the P25 LC header.

The SDRTrunk JAR included in this repo has been patched to forward `lat` and `lon` fields in the Broadcastify Calls POST when GPS data is present. If your city enables LRRP, coordinates flow into `calls.lat` / `calls.lon` automatically — no further changes needed.

Norfolk, VA does not currently broadcast LRRP. If yours does, you'll start seeing GPS data immediately.

---

## Ops Notes

- **Workers restart needed** after any `app/*.py` change — Flask auto-reloads but workers don't
- **PID lockfile** at `/tmp/sdrtrunk-workers.pid` prevents accidental double-launch
- **Geocode hit rate** ~50-60% — limited by Whisper transcript quality; addresses spoken clearly geocode reliably, "I'm en route" does not
- **Archive grows at ~400MB/day** at 600 calls/hour (16kbps CBR MP3)
- See `NAPKIN.md` for hard-won lessons on Whisper tuning, the Broadcastify protocol, and incident threading edge cases

---

## Roadmap

See [`IMPROVEMENTS.md`](IMPROVEMENTS.md) for the full backlog. Top items:

- [ ] Virginia Beach + Chesapeake address databases (adjacent cities, same P25 system)
- [ ] OpenClaw / webhook push notifications when priority alerts fire
- [ ] systemd services for reboot survivability
- [ ] Whisper `initial_prompt` injection for city-specific vocabulary
- [ ] Incident AI summary on close (2-sentence Gemini Flash digest)
- [ ] "What just happened" on-demand 30-minute activity digest

---

## Why PostgreSQL?

Because it already does everything:
- `pgvector` for 1536-dim embeddings and cosine similarity search
- `PostGIS` for geo proximity queries and incident location
- `pg_trgm` for fuzzy address matching against 78k address points
- `LISTEN/NOTIFY` for zero-polling worker pipeline
- Advisory locks for race-condition-safe incident creation
- Materialized views for call volume baselines

No Redis. No Elasticsearch. No separate vector DB. Just Postgres.

---

## Acknowledgments

Built on top of [SDRTrunk](https://github.com/DSheirer/sdrtrunk) by Dennis Sheirer — the best P25 decoder available, period.

Talkgroup data from [RadioReference](https://www.radioreference.com/).  
Address data from [City of Norfolk Open Data](https://data.norfolk.gov/).  
Maps: CartoDB Dark Matter + Leaflet.

---

## License

MIT. Go build something cool.

*— built in Norfolk, VA, listening to the city*
