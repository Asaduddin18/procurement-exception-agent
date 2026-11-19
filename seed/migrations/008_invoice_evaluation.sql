CREATE TABLE ap_document_evaluations (
    run_id uuid PRIMARY KEY REFERENCES processed_events (run_id),
    realm_id text NOT NULL CHECK (realm_id ~ '^[0-9]+$'),
    document_type text NOT NULL CHECK (document_type IN ('INVOICE', 'STATEMENT', 'OTHER')),
    evidence jsonb NOT NULL,
    findings jsonb NOT NULL,
    policy jsonb NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT now()
);
