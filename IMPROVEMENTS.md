# IMPROVEMENTS.md — sdrtrunk-pg

Active backlog. Top items are highest priority.
Last updated: 2026-03-21

---

## 🔴 High Priority

### Multi-city / Open Source Readiness
- [x] ~~City config file + street corrections + landmarks~~ ✅
- [x] ~~Remove all hardcoded Norfolk references from app code~~ ✅
- [ ] **README screenshots** — grab screenshots of light mode, search, detail panel for the README
- [ ] **`.env.example`** — complete example with all config vars documented

### Dispatcher-Centric Threading (alternative model)
The current threading model groups calls into incidents by geo/radio/TG proximity. This works but struggles with dispatch channels (NPD 2nd Main etc.) where one dispatcher handles sequential unrelated calls that get falsely merged.

**The insight:** dispatchers ARE the natural thread. Radio 16041 manages one call at a time — their stream is already a perfect sequential log. Addresses become lightweight map markers linked to moments in the dispatcher's timeline, not heavyweight incident blobs.

- [ ] **Dispatcher view tab** — new sidebar tab showing each active dispatcher's sequential call stream (radio_ids <50000 with high call counts). Each entry shows the call transcript, FROM unit, and any extracted address as a clickable map marker.
- [ ] **Dispatcher call segmentation** — detect when a dispatcher moves to a new call: "clear" codes, new unit addressing, topic change. Split their stream into logical segments.
- [ ] **Hybrid model** — keep current incident model for geo-located events (it works well for multi-unit responses like structure fires). Add dispatcher view as a second lens on the same data. Two tabs, same calls, different groupings.
- [ ] **Address pins** — map markers that link to the specific dispatcher stream entry where the address was mentioned, not to a blob incident. Lightweight, precise, no false grouping.

### Incident Intelligence
- [ ] **Clear-code auto-close** — detect "10-8", "back in service", "clear", "show me code 4" in transcript → auto-close the incident immediately instead of waiting 20-min stale timer. This is the #1 thread quality issue: dispatch channels carry sequential unrelated calls that get merged because the previous incident didn't close fast enough.
- [ ] **Incident AI summary** — when an incident closes, Gemini Flash reads all transcripts and writes a 2-sentence summary (e.g. "Single-vehicle accident at Church and 18th, Nissan Sentra, tow dispatched, cleared at 15:24"). Store in `incidents.summary`, show in detail panel and sidebar card.
- [ ] **Upload auth token** — replace the reverted API key check on PUT with an HMAC signed token embedded in the upload URL from step 1. SDRTrunk can't send the key on step 2, but a signed URL proves step 1 was auth'd.

### Transcription Quality
- [ ] **Whisper `initial_prompt` injection** — prepend Norfolk street vocabulary as Whisper context: "Norfolk Virginia P25 police fire EMS radio: Military Highway, Granby Street, Little Creek Road, Brambleton Avenue, Colley Avenue, Norview Avenue, Tidewater Drive, Princess Anne Road, Monticello Avenue, Azalea Garden Road" — primes the decoder for local proper nouns
- [ ] **Whisper model upgrade** — test `medium` model (currently `small`); expected accuracy boost on garbled addresses at ~3x latency cost

### Geocoding
- [ ] **Virginia Beach + Chesapeake + Portsmouth address DBs** — adjacent cities on the same P25 system; their calls are being captured but addresses won't match Norfolk-only DB
- [ ] **Neighborhood polygon overlay** — PostGIS join to Norfolk neighborhood shapefile from data.norfolk.gov; label incidents by neighborhood name in sidebar

---

## 🟡 Medium Priority

### Analytics & Views (inspired by POC `sdrtrunk_recording_parser`)
- [ ] **TG call stats view** — calls per TG per day (POC had `vw_call_stats`); expose as `/api/analytics/tg-daily`
- [ ] **Top TGs all-time** — ranked by total calls across all days (POC had `vw_top`); already partially in Stats tab but needs persistence across DB wipes
- [ ] **Yesterday's calls** — quick comparison view (POC had `vw_yesterday_calls`); useful for "was last night busier than usual?"
- [ ] **Current day stats** — live updating (POC had `vw_stats_current`); partially covered by Stats tab
- [ ] **Unit activity profiles** — radio_id 5556259 was on 3 incidents today; build per-unit dashboard: incidents responded to, TGs used, active hours, most common dispatch pair
- [ ] **Shift change detection** — call volume patterns reveal when NPD/NFD shifts change; surface as a marker on the activity chart
- [ ] **Hot spot identification** — "3 incidents on the 2400 block of Granby in the last week"; cluster geocoded incidents by proximity + time window
- [ ] **Cross-day trends** — which TGs are getting busier over time? Weekly/monthly call volume trends per category
- [ ] **Talkgroup activity heatmap** — per-TG call frequency calendar view (GitHub contribution graph style)

### Alerting & Notifications
- [ ] **OpenClaw push notification** — wire `ALERT_WEBHOOK_URL` to OpenClaw webhook; keyword alerts (shots fired, officer down, mayday) arrive as phone push notifications within seconds
- [ ] **Alert deduplication** — don't re-fire same rule on same incident within 5 minutes (partially fixed with LIKE pattern, needs proper incident_id tracking)
- [ ] **Fire/EMS priority escalation** — automatic PRIORITY card escalation when Fire/EMS incident exceeds 10 calls in 5 minutes

### UI / Dashboard
- [ ] **Mobile layout** — current layout breaks on phone; at minimum make feed readable on mobile
- [ ] **Incident share link** — `/incident/{id}` permalink renders a static summary for sharing in Signal/Discord
- [ ] **Incident history view** — closed incidents timeline for today, filterable by category; "what happened today" digest
- [ ] **Search result → map fly** — clicking semantic search result flies map to the call's incident location if geocoded
- [ ] **Heatmap time scrubber** — slider to show heatmap at different time windows
- [ ] **"What just happened" digest** — on-demand AI summary of last 30 minutes across all active incidents

### Temporal / Historical
- [ ] **Temporal playback** — "show me what was happening at 22:00 last Tuesday" — replay the map state at any historical point, watch incidents unfold in fast-forward
- [ ] **Daily digest email** — automated morning summary of overnight activity: top incidents, call counts, notable transcripts
- [ ] **Incident archive browser** — searchable closed incident history with full transcript timelines and audio playback

---

## 🟢 Low Priority / Nice to Have

### Infrastructure
- [ ] **systemd services** — Flask app + workers survive reboot without manual restart
- [ ] **DB connection pooling** — `psycopg2.pool.ThreadedConnectionPool` instead of new connection per request
- [ ] **Archive retention** — keep MP3s 90 days, transcripts/embeddings forever; nightly cleanup cron
- [ ] **Metrics endpoint** — `/api/metrics` Prometheus-compatible for basic monitoring
- [ ] **Multi-system support** — `system_id` is stored but not used for display separation; useful when multiple P25 systems are monitored

### GPS (if municipality enables LRRP)
- [ ] **SDRTrunk JAR is patched and ready** — `lat`/`lon` fields flow automatically if P25 LRRP is enabled
- [ ] **Promote incident location from GPS** — use GPS coords for incident location instead of geocoded transcript address (more accurate)

### Community / Open Source
- [ ] **RadioReference TG sync** — periodic refresh of talkgroup metadata from RadioReference API
- [ ] **Multi-city federation** — shared corrections dicts, shared Whisper improvements across instances running in different cities
- [ ] **CAD correlation** — cross-reference scanner transcripts with official police incident reports from city open data portals; your system hears the call before the public report exists
- [ ] **Radio unit social graph** — who talks to whom across shifts, weeks, months; network analysis of the P25 system

---

## ✅ Completed

### 2026-03-20 (Day 1 — the big build)

**Infrastructure & Pipeline:**
- [x] Two-step Broadcastify protocol (POST metadata + PUT audio)
- [x] Whisper tuning for scanner audio (language, thresholds, fp16)
- [x] Configurable embedding provider (gemini/openai/local)
- [x] Archive on permanent NVMe storage
- [x] Worker PID lockfile — prevents zombie duplicate worker processes
- [x] SDRTrunk JAR patched — lat/lon GPS fields added to Broadcastify POST
- [x] Talkgroup import from SDRTrunk playlist XML
- [x] Talkgroup category fix — 26 City Services TGs mis-categorized as Airport

**Incident Threading v2:**
- [x] Race condition fixed (DB UNIQUE anchor_call_id + ON CONFLICT)
- [x] Category-scoped radio joining (police can't merge with fire)
- [x] radio_id=0 excluded from anchor_radio
- [x] ±3min TG window (tightened from ±5)
- [x] 20min stale closer (bumped from 15 for NSO)
- [x] Geo guard on tg_window — rejects joins >1km apart (fixes dispatch channel cross-contamination)
- [x] Incident address updates to latest geocoded call, not first-only
- [x] Incident threading runs for ALL calls regardless of transcript content

**Geocoding:**
- [x] Norfolk address DB — 78,546 addresses from data.norfolk.gov with pg_trgm fuzzy matching
- [x] Local DB lookup → intersection centroid → Nominatim fallback pipeline
- [x] Whisper correction dict (45 entries): Molotowary→Military, Grandie→Granby, Melthrad→Military, etc.
- [x] Number dictation collapse: "7-9-3-6" → "7936", "8.5-3" → "853"
- [x] Abbreviation expansion with negative lookaheads
- [x] Thread-safe geocode cache; transient errors not cached
- [x] Entity prompt: skip bare apartment numbers, combine dictated numbers
- [x] 64% geocode hit rate (up from ~3% on day 1)

**Command Center UI:**
- [x] Full ground-up UI rebuild: header KPIs, filter bar, sidebar tabs, map
- [x] Stats tab: stacked activity chart, top TG bars, category breakdown
- [x] Semantic search tab (pgvector cosine similarity)
- [x] Priority incident card (urgency-scored, Fire/EMS bonus, 5-min rate)
- [x] Category filter pills + time window pills
- [x] 11-category system (Police/Fire/EMS/Military/Government/City Services/Transportation/Schools/Airport/Interop/Other)
- [x] Light/dark theme toggle with map tile swap (localStorage persistent)

**Incident Detail Panel:**
- [x] FROM/TO labels on call rows (P25 protocol semantics)
- [x] Unit Activity Timeline — horizontal bar chart per radio, dispatchers in orange
- [x] Conversation Graph — inferred from sequential keying <10s, bidirectional merge
- [x] Unit chips distinguish dispatchers (📡) from field units
- [x] Collapsible sections (Units, Timeline, Conversations)
- [x] Audio playback with auto-scroll, speaking unit pulse, timeline glow, conversation highlight
- [x] ⏹ Stop button
- [x] ⬇ Download MP3 — concatenates all incident audio via ffmpeg, background merge job

**Alerts:**
- [x] Actionable alert bar — each alert is clickable, navigates to incident or shows TG calls
- [x] Volume spike rule correctly matches by TG category
- [x] Materialized view baseline refresh in alert loop

**Security/Correctness (from code review):**
- [x] Timing-safe API key comparison (hmac.compare_digest)
- [x] MAX_CONTENT_LENGTH = 50MB on uploads
- [x] ARCHIVE_ROOT path validation on upload + audio stream
- [x] LLM prompt injection mitigation (XML transcript delimiters)
- [x] Upload auth reverted (SDRTrunk doesn't send key on step 2; signed token is the right fix)
- [x] Gemini entity extraction: guard against non-dict items in response
- [x] incidents_geo LIMIT 500
- [x] Closed map circles clickable (opacity .45, min 18px target, pointer-events:auto)

**Documentation:**
- [x] README rewrite — hero screenshot, multi-city instructions, architecture diagram, full API reference
- [x] SDRTrunk setup guide (docs/sdrtrunk-setup.md)
- [x] GPS patch as portable .patch file with apply instructions
- [x] NAPKIN.md updated with all session lessons
- [x] City config system: config.yaml + street_corrections.json + landmarks.json
- [x] Generic address DB loader (scripts/load_address_db.py) — Socrata, ArcGIS, CSV
- [x] Zero city-specific hardcoding in app code or templates

**Data cleanup:**
- [x] 2,119 missing-audio calls from auth-fix period: file_path cleared to ''
