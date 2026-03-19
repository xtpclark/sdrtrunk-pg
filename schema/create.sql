-- sdrtrunk-pg schema
-- Requires: PostgreSQL 15+, postgis, pgvector, pgcrypto

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------
-- Core calls table
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calls (
    id               bigserial PRIMARY KEY,
    tg               integer NOT NULL,
    system_id        text,
    radio_id         text,
    ts               timestamptz NOT NULL,
    duration_sec     float,
    freq_hz          bigint,
    file_path        text NOT NULL,
    received_at      timestamptz DEFAULT now(),
    -- Transcription
    transcript       text,
    transcript_model text,
    transcribed_at   timestamptz,
    -- Embedding
    embedding        vector(1536),
    embedded_at      timestamptz
);

CREATE INDEX IF NOT EXISTS calls_tg_idx         ON calls (tg);
CREATE INDEX IF NOT EXISTS calls_ts_idx         ON calls (ts);
CREATE INDEX IF NOT EXISTS calls_tg_ts_idx      ON calls (tg, ts);
CREATE INDEX IF NOT EXISTS calls_transcript_idx ON calls USING gin(to_tsvector('english', coalesce(transcript, '')));

-- Deferred: embedding index created after first data load
-- CREATE INDEX calls_embedding_idx ON calls USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- -----------------------------------------------------------------------
-- Talkgroup directory (imported from RadioReference CSV)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS talkgroups (
    tg_decimal  integer PRIMARY KEY,
    alpha_tag   text,
    description text,
    category    text,
    tag         text,
    mode        text,
    system_id   text
);

-- -----------------------------------------------------------------------
-- Sites (from RadioReference)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sites (
    site_id     serial PRIMARY KEY,
    system_id   text,
    rfss        text,
    site_dec    text,
    site_hex    text,
    site_nac    text,
    description text,
    county      text,
    lat         float,
    lon         float
);

-- -----------------------------------------------------------------------
-- Entities extracted from transcripts (addresses, units, codes)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS call_entities (
    id           bigserial PRIMARY KEY,
    call_id      bigint REFERENCES calls(id) ON DELETE CASCADE,
    entity_type  text NOT NULL,  -- 'address', 'unit', 'code', 'location'
    value        text NOT NULL,
    lat          float,
    lon          float,
    geom         geometry(Point, 4326),
    confidence   float,
    created_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS call_entities_call_id_idx ON call_entities (call_id);
CREATE INDEX IF NOT EXISTS call_entities_geom_idx    ON call_entities USING gist (geom);
CREATE INDEX IF NOT EXISTS call_entities_type_idx    ON call_entities (entity_type);

-- -----------------------------------------------------------------------
-- Merge jobs
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_jobs (
    id           bigserial PRIMARY KEY,
    label        text,
    tg           integer,
    window_start timestamptz,
    window_end   timestamptz,
    call_count   integer,
    file_path    text,
    status       text NOT NULL DEFAULT 'pending',  -- pending, running, done, failed
    error        text,
    created_at   timestamptz DEFAULT now(),
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS merge_jobs_status_idx ON merge_jobs (status);

-- -----------------------------------------------------------------------
-- Alert rules
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_rules (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    rule_type   text NOT NULL,  -- 'keyword', 'volume_spike', 'radio_id', 'category'
    config      jsonb NOT NULL DEFAULT '{}',
    enabled     boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- -----------------------------------------------------------------------
-- Alert log
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    id         bigserial PRIMARY KEY,
    rule_id    integer REFERENCES alert_rules(id),
    call_id    bigint REFERENCES calls(id),
    message    text,
    fired_at   timestamptz DEFAULT now(),
    notified   boolean DEFAULT false
);

CREATE INDEX IF NOT EXISTS alerts_notified_idx ON alerts (notified) WHERE notified = false;
CREATE INDEX IF NOT EXISTS alerts_fired_at_idx ON alerts (fired_at);

-- -----------------------------------------------------------------------
-- Config / preferences
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pref (
    key         text PRIMARY KEY,
    value       text,
    description text
);

-- -----------------------------------------------------------------------
-- Views
-- -----------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_calls AS
    SELECT c.*,
           t.alpha_tag,
           t.description AS tg_description,
           t.category,
           t.tag,
           to_char(c.ts AT TIME ZONE 'America/New_York', 'HH24')::integer AS hour_of_day,
           extract(dow FROM c.ts)::integer AS day_of_week,
           date_trunc('hour', c.ts) AS hour_bucket
    FROM calls c
    LEFT JOIN talkgroups t ON t.tg_decimal = c.tg;

-- Call volume baseline (refresh nightly)
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_call_volume_baseline AS
    SELECT tg,
           extract(dow FROM ts)::integer  AS dow,
           extract(hour FROM ts)::integer AS hod,
           count(*)                        AS call_count,
           avg(duration_sec)               AS avg_duration
    FROM calls
    GROUP BY tg, extract(dow FROM ts), extract(hour FROM ts);

CREATE UNIQUE INDEX IF NOT EXISTS mv_cvb_idx ON mv_call_volume_baseline (tg, dow, hod);
