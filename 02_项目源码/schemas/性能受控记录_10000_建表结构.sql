CREATE TABLE events(id INTEGER PRIMARY KEY,region TEXT NOT NULL,status TEXT NOT NULL,amount_cents INTEGER NOT NULL);
CREATE INDEX idx_events_region_status ON events(region,status);
