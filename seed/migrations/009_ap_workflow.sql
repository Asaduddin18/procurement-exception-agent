ALTER TABLE procurement_runs ADD COLUMN document_type text NOT NULL DEFAULT 'PO'
    CHECK (document_type IN ('PO', 'INVOICE'));
ALTER TABLE procurement_runs DROP CONSTRAINT procurement_runs_status_check;
ALTER TABLE procurement_runs ADD CONSTRAINT procurement_runs_status_check CHECK (status IN (
    'QUEUED', 'PROCESSING', 'EXTRACTED', 'AWAITING_REVIEW', 'APPROVED', 'DISPUTED',
    'REJECTED', 'FAILED', 'SYNCED_TO_QBO'
));
ALTER TABLE procurement_jobs DROP CONSTRAINT procurement_jobs_kind_check;
ALTER TABLE procurement_jobs ADD CONSTRAINT procurement_jobs_kind_check
    CHECK (kind IN ('PROCESS', 'EVALUATE', 'RESUME', 'QBO_POST'));
