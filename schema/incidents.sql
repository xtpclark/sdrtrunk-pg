-- Incident threading tables
-- Requires: postgis extension (already in create.sql)

CREATE TABLE IF NOT EXISTS incidents (
    id              bigserial PRIMARY KEY,
    anchor_call_id  bigint REFERENCES calls(id),
    address         text,
    location        geometry(Point, 4326),
    category        text,           -- from anchor call's talkgroup category
    opened_at       timestamptz NOT NULL,
    closed_at       timestamptz,
    last_activity   timestamptz NOT NULL,
    status          text NOT NULL DEFAULT 'active',  -- active, closed
    summary         text,           -- AI-generated later
    call_count      integer DEFAULT 1,
    unit_count      integer DEFAULT 1
);

CREATE INDEX IF NOT EXISTS incidents_status_idx     ON incidents (status);
CREATE INDEX IF NOT EXISTS incidents_opened_at_idx  ON incidents (opened_at);
CREATE INDEX IF NOT EXISTS incidents_location_idx   ON incidents USING gist (location);

CREATE TABLE IF NOT EXISTS incident_calls (
    id           bigserial PRIMARY KEY,
    incident_id  bigint REFERENCES incidents(id) ON DELETE CASCADE,
    call_id      bigint REFERENCES calls(id),
    radio_id     text,
    joined_at    timestamptz DEFAULT now(),
    join_reason  text,   -- 'anchor', 'anchor_radio', 'tg_window', 'geo_proximity'
    UNIQUE (incident_id, call_id)
);

CREATE INDEX IF NOT EXISTS ic_incident_idx ON incident_calls (incident_id);
CREATE INDEX IF NOT EXISTS ic_call_idx     ON incident_calls (call_id);
CREATE INDEX IF NOT EXISTS ic_radio_idx    ON incident_calls (radio_id);
