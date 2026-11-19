ALTER TABLE qbo_purchase_order_lines ADD COLUMN is_active boolean NOT NULL DEFAULT true;

CREATE TABLE qbo_bill_attachments (
    invoice_id uuid PRIMARY KEY REFERENCES vendor_invoices (invoice_id),
    qbo_attachable_id text CHECK (btrim(qbo_attachable_id) <> ''),
    started_at timestamptz NOT NULL DEFAULT now()
);
