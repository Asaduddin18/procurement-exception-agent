CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE vendors (
    vendor_id text PRIMARY KEY,
    vendor_name text NOT NULL UNIQUE,
    payment_terms_days smallint NOT NULL CHECK (payment_terms_days >= 0),
    contract_reference text NOT NULL
);

CREATE TABLE price_catalog (
    vendor_id text NOT NULL REFERENCES vendors (vendor_id),
    sku text NOT NULL,
    description text NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    unit text NOT NULL CHECK (btrim(unit) <> ''),
    unit_price numeric(18, 4) NOT NULL CHECK (
        unit_price > 0 AND unit_price <> 'NaN'::numeric
    ),
    PRIMARY KEY (vendor_id, sku)
);

CREATE TABLE procurement_runs (
    run_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL UNIQUE,
    category text NOT NULL CHECK (btrim(category) <> ''),
    region text NOT NULL CHECK (region ~ '^[A-Z]{2}$'),
    status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN (
        'QUEUED', 'PROCESSING', 'AWAITING_REVIEW', 'APPROVED', 'DISPUTED', 'REJECTED', 'FAILED'
    )),
    vendor_id text REFERENCES vendors (vendor_id),
    po_number text,
    currency text CHECK (currency ~ '^[A-Z]{3}$'),
    grand_total numeric(18, 2) CHECK (grand_total >= 0 AND grand_total <> 'NaN'::numeric),
    decision_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE processed_events (
    sha256 text PRIMARY KEY CHECK (sha256 ~ '^[a-f0-9]{64}$'),
    run_id uuid NOT NULL UNIQUE REFERENCES procurement_runs (run_id),
    source_id text,
    received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE procurement_inputs (
    run_id uuid PRIMARY KEY REFERENCES procurement_runs (run_id),
    pdf_bytes bytea NOT NULL CHECK (octet_length(pdf_bytes) > 0)
);

CREATE TABLE procurement_jobs (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES procurement_runs (run_id),
    kind text NOT NULL CHECK (kind IN ('PROCESS', 'RESUME')),
    status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN (
        'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED'
    )),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_expires_at timestamptz,
    CHECK (
        (status = 'RUNNING' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'RUNNING' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    UNIQUE (run_id, kind)
);

CREATE INDEX procurement_jobs_available_idx ON procurement_jobs (available_at)
    WHERE status = 'QUEUED';
CREATE INDEX procurement_jobs_lease_idx ON procurement_jobs (lease_expires_at)
    WHERE status = 'RUNNING';

CREATE TABLE audit_logs (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES procurement_runs (run_id),
    event_key text NOT NULL CHECK (btrim(event_key) <> ''),
    event_type text NOT NULL CHECK (btrim(event_type) <> ''),
    actor_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, event_key)
);

CREATE TABLE sop_policy_versions (
    policy_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    effective_date date NOT NULL,
    category text NOT NULL CHECK (btrim(category) <> ''),
    region text NOT NULL CHECK (region ~ '^[A-Z]{2}$'),
    title text NOT NULL,
    PRIMARY KEY (policy_id, version)
);

CREATE INDEX sop_policy_active_scope_idx
    ON sop_policy_versions (region, category, effective_date)
    WHERE status = 'ACTIVE';

CREATE TABLE sop_policy_rules (
    policy_id text NOT NULL,
    version integer NOT NULL,
    rule_code text NOT NULL,
    rule_value numeric(18, 4) NOT NULL CHECK (
        rule_value >= 0 AND rule_value <> 'NaN'::numeric
    ),
    currency text CHECK (currency ~ '^[A-Z]{3}$'),
    PRIMARY KEY (policy_id, version, rule_code),
    FOREIGN KEY (policy_id, version) REFERENCES sop_policy_versions (policy_id, version),
    CHECK (
        (rule_code = 'PAYMENT_TERMS_DAYS' AND currency IS NULL
            AND rule_value = trunc(rule_value))
        OR (rule_code = 'FINANCE_DIRECTOR_CAP' AND currency IS NOT NULL)
        OR (rule_code = 'PRICE_VARIANCE_FRACTION' AND currency IS NULL AND rule_value <= 1)
    )
);

CREATE TABLE sop_policy_chunks (
    policy_id text NOT NULL,
    version integer NOT NULL,
    chunk_number integer NOT NULL CHECK (chunk_number > 0),
    source_location text NOT NULL CHECK (btrim(source_location) <> ''),
    content text NOT NULL CHECK (btrim(content) <> ''),
    embedding_space text NOT NULL CHECK (btrim(embedding_space) <> ''),
    embedding vector(1536) NOT NULL,
    PRIMARY KEY (policy_id, version, chunk_number),
    FOREIGN KEY (policy_id, version) REFERENCES sop_policy_versions (policy_id, version),
    CHECK (vector_norm(embedding) > 0)
);
