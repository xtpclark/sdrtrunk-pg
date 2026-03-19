-- Seed default alert rules
INSERT INTO alert_rules (name, rule_type, config) VALUES
    ('Structure Fire', 'keyword',
     '{"keywords": ["structure fire", "working fire", "flames showing", "fully involved"]}'),
    ('Shots Fired', 'keyword',
     '{"keywords": ["shots fired", "shooting", "gunshot", "man with a gun"]}'),
    ('Officer Down', 'keyword',
     '{"keywords": ["officer down", "10-99", "mayday", "assist officer"]}'),
    ('Medical Emergency', 'keyword',
     '{"keywords": ["cardiac arrest", "unresponsive", "not breathing", "code blue"]}'),
    ('Vehicle Pursuit', 'keyword',
     '{"keywords": ["in pursuit", "vehicle pursuit", "pursuit in progress"]}'),
    ('Police Volume Spike', 'volume_spike',
     '{"category": "Police", "threshold_multiplier": 2.5, "window_minutes": 60}'),
    ('Fire Volume Spike', 'volume_spike',
     '{"category": "Fire/EMS", "threshold_multiplier": 2.0, "window_minutes": 60}')
ON CONFLICT DO NOTHING;
