# IMPROVEMENTS.md — sdrtrunk-pg

Active backlog. Top items are highest priority.

## Active

### UI / Dashboard
- [ ] Unlocated thread sidebar — show radio clusters without geocode, promote to map pin when address appears
- [ ] Incident detail panel — slide-in from right, full transcript timeline, audio playlist, unit badges
- [ ] Live feed bottom pane — scrolling call feed, color-coded by category, click → highlight thread
- [ ] Metric flash animations — call count / TG count / incident count flash on change
- [ ] Map layer toggles — heatmap / incident pins / call dots independently
- [ ] Mobile-responsive layout (at least not broken on phone)

### Incident Engine
- [ ] Create incidents without requiring geocode — radio cluster alone should open a thread
- [ ] Promotion logic — unlocated incident gets map pin when a geocoded call joins it
- [ ] Incident summary — AI-generated summary after close (Gemini Flash, short prompt)
- [ ] 10-8 / clear codes in transcript → auto-close incident
- [ ] Multi-TG incident grouping — Fire + EMS + Police on same event (currently TG-siloed)

### Transcription
- [ ] TG-specific skip list — TG 653 (NFD Alerting) is pure tones, skip transcription
  `INSERT INTO pref VALUES ('skip_transcription_tgs', '653')`
- [ ] Whisper prompt injection — prepend "Norfolk Virginia police radio: " to improve P25 vocab
- [ ] Re-transcribe backlog — ~1,400 calls from before tuning fix never got good transcription

### Geocoding
- [ ] Whisper mishearing correction — "East Low Creek" → "East Little Creek" lookup table
- [ ] Cross-street geocoding — "Main and Elm" → Nominatim intersection query
- [ ] Neighborhood polygon overlay — PostGIS join to label calls by neighborhood

### Alerting
- [ ] OpenClaw webhook → push notification when keyword alert fires
- [ ] Volume spike alerting — needs `REFRESH MATERIALIZED VIEW mv_call_volume_baseline` nightly
- [ ] Emergency flag — detect "mayday", "officer down", "shots fired" → high priority alert
- [ ] Alert deduplication — don't re-alert same incident within 5 min

### Data / Backfill
- [ ] Embed backfill — ~1,400 transcribed calls without embeddings yet
- [ ] Entity extraction backfill — run Gemini entity extract on all existing transcripts
- [ ] Geocode backfill — re-run after geocoder fixes (bounded, city context)

### Infrastructure
- [ ] systemd service — Flask app + workers should survive reboot
- [ ] `REFRESH MATERIALIZED VIEW mv_call_volume_baseline` nightly cron
- [ ] Archive retention policy — keep MP3s 90 days, transcripts forever
- [ ] Virginia Beach P25 — add VB talkgroups to playlist, wire broadcastChannel
- [ ] Chesapeake P25 — same

## Completed

- [x] Two-step Broadcastify protocol (POST metadata + PUT audio)
- [x] Whisper tuning for scanner audio (language, thresholds, fp16)
- [x] Configurable embedding provider (gemini/openai/local)
- [x] Geocoder bounded to Hampton Roads, address-only, city context
- [x] Incident threading (geo proximity, radio_id, TG window)
- [x] Map layer with pulsing incident bubbles
- [x] Talkgroup import from SDRTrunk playlist XML
- [x] Archive moved to permanent NVMe storage
- [x] NAPKIN.md with lessons learned
