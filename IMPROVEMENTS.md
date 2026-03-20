# IMPROVEMENTS.md — sdrtrunk-pg

Active backlog. Top items are highest priority.
Last updated: 2026-03-20

---

## 🔴 High Priority

### Multi-city / Open Source Readiness
Move all city-specific hardcoding into config files so anyone can deploy against their P25 system.

- [ ] **City config file** — `data/cities/{slug}/config.yaml` with bbox, center, timezone, geocode_context, address_db source URL + field mappings
- [ ] **Street corrections** — move `_CORRECTIONS` dict from `geocode.py` to `data/cities/norfolk-va/street_corrections.json` (loaded at startup from active city config)
- [ ] **Landmarks** — move named location hints ("NSO Courts", "MacArthur Center") to `data/cities/norfolk-va/landmarks.json`; used in entity extraction prompt
- [ ] **bbox from config** — `_BBOX_*` and `_CITY_CONTEXT` in `geocode.py` read from city config, not hardcoded
- [ ] **GPS sanity bbox** — `ingest.py` derives sanity-check bbox from city config instead of hardcoded 35–38.5°N
- [ ] **Map center** — `NORFOLK` constant in `map.html` comes from Flask template variable set by city config
- [ ] **Entity prompt** — "Norfolk, Virginia dispatcher" and streets hint list come from city config
- [ ] **Address DB loader** — `scripts/load_address_db.py` reads source URL + field mappings from city config; handles Socrata CSV and ArcGIS GeoJSON formats
- [ ] **README** — document how to add a new city (Detroit, Portland OR, Charlotte, etc.)

### Geocoding
- [ ] **Virginia Beach address DB** — download from `gis.data.vbgov.com/datasets/address-points` and load into `address_db` with `city='Virginia Beach'`; VB TGs are active on the Norfolk P25 system
- [ ] **Chesapeake address DB** — same; Chesapeake FD/PD are on Hampton Roads P25
- [ ] **Portsmouth address DB** — same; NSY Portsmouth TGs captured
- [ ] **Intersection scoring** — `_local_intersection()` currently averages all nearby points; refine to find true intersection centroid using street centerline data
- [ ] **Neighborhood polygon overlay** — PostGIS join to label incidents by neighborhood name (Norfolk neighborhood shapefile available from data.norfolk.gov)

### Incident Threading
- [ ] **Multi-agency incident grouping** — Fire + EMS + Police on same event currently thread separately; if geo_proximity match exists across categories, create a parent incident link
- [ ] **10-8 / clear code auto-close** — detect "10-8", "clear", "back in service" → mark incident closed early rather than waiting 20-min stale timer
- [ ] **Incident AI summary** — after close, run Gemini Flash short prompt: "Summarize this incident in 2 sentences" → store in `incidents.summary`; show in detail panel

---

## 🟡 Medium Priority

### Transcription Quality
- [ ] **Whisper prompt injection** — prepend `"Norfolk Virginia P25 police fire EMS radio: "` to Whisper `initial_prompt`; improves recognition of scanner vocabulary and local proper nouns
- [ ] **TG skip list** — pure-tone TGs (NFD Alerting 653, etc.) waste Whisper time; add `skip_transcription_tgs` config option
- [ ] **Re-transcribe with better model** — ~1,400 calls from session start used `base` model; re-run with `small` for better accuracy on existing archive

### Alerting
- [ ] **OpenClaw push notification** — wire `ALERT_WEBHOOK_URL` to OpenClaw webhook so keyword alerts (shots fired, officer down, mayday) arrive as phone notifications
- [ ] **Alert deduplication** — don't re-fire same rule on same incident within 5 minutes
- [ ] **Volume baseline** — `REFRESH MATERIALIZED VIEW mv_call_volume_baseline` nightly via cron so volume-spike rules have a valid baseline
- [ ] **Fire/EMS priority escalation** — automatic PRIORITY card escalation when Fire/EMS incident exceeds 10 calls in 5 minutes

### UI / Dashboard
- [ ] **Mobile layout** — current layout breaks on phone; at minimum make the feed readable on a mobile screen
- [ ] **Incident share link** — `/incident/{id}` permalink that renders a static summary (for sharing in Signal/Discord with other scanner nerds)
- [ ] **Incident history view** — closed incidents timeline for today, filterable by category
- [ ] **Search result → map fly** — clicking a semantic search result should fly the map to the call's incident location if geocoded
- [ ] **Heatmap time scrubber** — slider to show heatmap at different time windows (last 30m / 1h / 4h)
- [ ] **"What just happened" digest** — on-demand AI summary of last 30 minutes of activity across all active incidents

---

## 🟢 Low Priority / Nice to Have

### Infrastructure
- [ ] **systemd services** — Flask app + workers should survive reboot without manual restart
- [ ] **Archive retention** — keep MP3s 90 days, transcripts/embeddings forever; nightly cleanup cron
- [ ] **Metrics endpoint** — `/api/metrics` Prometheus-compatible for basic monitoring
- [ ] **Multi-system support** — currently assumes one P25 system; `system_id` is stored but not used for display separation; useful when VB P25 is added

### GPS (if Norfolk ever enables LRRP)
- [ ] **SDRTrunk JAR is patched and ready** — `lat`/`lon` fields will flow from SDRTrunk POST → `calls` table automatically if P25 LRRP is enabled by the municipality
- [ ] **Promote incident location from GPS** — if a call has GPS coords, use those for incident location instead of geocoded transcript address

### Data
- [ ] **RadioReference TG sync** — periodic refresh of talkgroup metadata from RadioReference API (categories, descriptions, alpha tags change)
- [ ] **Talkgroup activity heatmap** — per-TG call frequency calendar view (think GitHub contribution graph)

---

## ✅ Completed (2026-03-20)

- [x] Two-step Broadcastify protocol (POST metadata + PUT audio)
- [x] Whisper tuning for scanner audio (language, thresholds, fp16)
- [x] Configurable embedding provider (gemini/openai/local)
- [x] Geocoder bounded to Hampton Roads with city context
- [x] Incident threading (geo proximity, radio_id, TG window)
- [x] Map layer with pulsing incident bubbles
- [x] Talkgroup import from SDRTrunk playlist XML
- [x] Archive on permanent NVMe storage
- [x] Full command center UI rebuild — stats tab, search tab, priority card, category filter, window filter, semantic search
- [x] Incident threading v2 — race condition fixed (DB UNIQUE constraint + ON CONFLICT), category-scoped radio joining, radio_id=0 excluded, ±3min TG window, 20min stale closer
- [x] Talkgroup category fix — 26 Norfolk City Services TGs mis-categorized as Airport, corrected
- [x] SDRTrunk JAR patched — lat/lon GPS fields added to Broadcastify POST (waiting on municipality)
- [x] Worker PID lockfile — prevents zombie duplicate worker processes
- [x] Norfolk address DB — 78,546 city addresses loaded from data.norfolk.gov; pg_trgm fuzzy matching for fast local geocoding (no rate limit, handles apartments, fuzzy street names)
- [x] Geocoder improvements — intersection lookup via local DB, Whisper correction dict, abbreviation expansion with negative lookaheads, Nominatim as fallback
- [x] Entity extraction prompt — Norfolk-specific, abbreviation normalization, artifact detection
- [x] Geocode backfill — 116 previously-failed addresses resolved with new geocoder
