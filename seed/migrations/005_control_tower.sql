ALTER TABLE processed_events
    ADD COLUMN duplicate_count bigint NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0);

CREATE INDEX procurement_runs_created_idx ON procurement_runs (created_at DESC, run_id DESC);
