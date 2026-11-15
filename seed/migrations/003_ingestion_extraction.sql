ALTER TABLE procurement_runs DROP CONSTRAINT procurement_runs_status_check;
ALTER TABLE procurement_runs
    ADD CONSTRAINT procurement_runs_status_check CHECK (status IN (
        'QUEUED', 'PROCESSING', 'EXTRACTED', 'AWAITING_REVIEW',
        'APPROVED', 'DISPUTED', 'REJECTED', 'FAILED'
    )),
    ADD COLUMN extracted_po jsonb,
    ADD COLUMN error_code text,
    ADD CONSTRAINT procurement_runs_extracted_po_check
        CHECK (extracted_po IS NULL OR jsonb_typeof(extracted_po) = 'object');
