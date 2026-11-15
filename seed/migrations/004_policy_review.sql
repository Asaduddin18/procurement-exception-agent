ALTER TABLE procurement_jobs DROP CONSTRAINT procurement_jobs_kind_check;
ALTER TABLE procurement_jobs ADD CONSTRAINT procurement_jobs_kind_check
    CHECK (kind IN ('PROCESS', 'EVALUATE', 'RESUME'));

ALTER TABLE sop_policy_rules
    ADD COLUMN source_chunk_number integer,
    ADD FOREIGN KEY (policy_id, version, source_chunk_number)
        REFERENCES sop_policy_chunks (policy_id, version, chunk_number);

CREATE TABLE procurement_resolutions (
    run_id uuid PRIMARY KEY REFERENCES procurement_runs (run_id),
    actor_id text NOT NULL CHECK (btrim(actor_id) <> ''),
    role text NOT NULL CHECK (role IN ('PROCUREMENT_REVIEWER', 'FINANCE_DIRECTOR')),
    action text NOT NULL CHECK (action IN ('APPROVE', 'DISPUTE', 'REJECT')),
    notes text NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO procurement_jobs (job_id, run_id, kind)
SELECT gen_random_uuid(), run_id, 'EVALUATE'
FROM procurement_runs WHERE status = 'EXTRACTED'
ON CONFLICT (run_id, kind) DO NOTHING;
