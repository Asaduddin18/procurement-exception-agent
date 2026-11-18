CREATE DOMAIN ap_quantity AS numeric
    CHECK (VALUE > 0 AND VALUE < 100000000000000 AND VALUE = round(VALUE, 4));

CREATE DOMAIN ap_money AS numeric
    CHECK (VALUE >= 0 AND VALUE < 100000000000000 AND VALUE = round(VALUE, 4));

CREATE TABLE qbo_purchase_orders (
    realm_id text NOT NULL CHECK (realm_id ~ '^[0-9]+$'),
    qbo_po_id text NOT NULL CHECK (btrim(qbo_po_id) <> ''),
    po_number text NOT NULL CHECK (btrim(po_number) <> ''),
    vendor_id text NOT NULL REFERENCES vendors (vendor_id),
    qbo_vendor_id text NOT NULL CHECK (btrim(qbo_vendor_id) <> ''),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    is_open boolean NOT NULL,
    synced_at timestamptz NOT NULL,
    PRIMARY KEY (realm_id, qbo_po_id),
    UNIQUE (realm_id, qbo_po_id, vendor_id, currency)
);
CREATE INDEX qbo_po_number_idx ON qbo_purchase_orders (realm_id, vendor_id, po_number);

CREATE TABLE qbo_purchase_order_lines (
    realm_id text NOT NULL,
    qbo_po_id text NOT NULL,
    qbo_line_id text NOT NULL CHECK (btrim(qbo_line_id) <> ''),
    qbo_item_id text NOT NULL CHECK (btrim(qbo_item_id) <> ''),
    sku text NOT NULL CHECK (btrim(sku) <> ''),
    unit text NOT NULL CHECK (btrim(unit) <> ''),
    ordered_quantity ap_quantity NOT NULL,
    unit_price ap_money NOT NULL,
    PRIMARY KEY (realm_id, qbo_po_id, qbo_line_id),
    FOREIGN KEY (realm_id, qbo_po_id) REFERENCES qbo_purchase_orders (realm_id, qbo_po_id)
);

CREATE TABLE goods_receipts (
    realm_id text NOT NULL,
    source_system text NOT NULL CHECK (btrim(source_system) <> ''),
    receipt_event_id text NOT NULL CHECK (btrim(receipt_event_id) <> ''),
    qbo_po_id text NOT NULL,
    qbo_line_id text NOT NULL,
    received_quantity ap_quantity NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (realm_id, source_system, receipt_event_id),
    FOREIGN KEY (realm_id, qbo_po_id, qbo_line_id)
        REFERENCES qbo_purchase_order_lines (realm_id, qbo_po_id, qbo_line_id)
);
CREATE INDEX goods_receipts_po_line_idx ON goods_receipts (realm_id, qbo_po_id, qbo_line_id);

CREATE TABLE vendor_invoices (
    invoice_id uuid PRIMARY KEY,
    run_id uuid NOT NULL UNIQUE REFERENCES processed_events (run_id),
    realm_id text NOT NULL CHECK (realm_id ~ '^[0-9]+$'),
    vendor_id text NOT NULL REFERENCES vendors (vendor_id),
    invoice_number text NOT NULL CHECK (btrim(invoice_number) <> ''),
    invoice_number_key text GENERATED ALWAYS AS (upper(btrim(invoice_number))) STORED,
    po_number text CHECK (btrim(po_number) <> ''),
    qbo_po_id text,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    freight_amount ap_money NOT NULL DEFAULT 0 CHECK (freight_amount = round(freight_amount, 2)),
    total_amount ap_money NOT NULL CHECK (total_amount = round(total_amount, 2)),
    status text NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN ('RECEIVED', 'RESERVED', 'POST_UNKNOWN', 'POSTED')),
    qbo_bill_id text CHECK (btrim(qbo_bill_id) <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (realm_id, vendor_id, invoice_number_key),
    UNIQUE (invoice_id, realm_id, qbo_po_id),
    UNIQUE (realm_id, qbo_bill_id),
    FOREIGN KEY (realm_id, qbo_po_id, vendor_id, currency)
        REFERENCES qbo_purchase_orders (realm_id, qbo_po_id, vendor_id, currency),
    CHECK ((status = 'POSTED') = (qbo_bill_id IS NOT NULL)),
    CHECK (status = 'RECEIVED' OR qbo_po_id IS NOT NULL)
);

CREATE TABLE invoice_line_items (
    invoice_id uuid NOT NULL REFERENCES vendor_invoices (invoice_id),
    line_number integer NOT NULL CHECK (line_number > 0),
    sku text NOT NULL CHECK (btrim(sku) <> ''),
    unit text NOT NULL CHECK (btrim(unit) <> ''),
    quantity ap_quantity NOT NULL,
    unit_price ap_money NOT NULL,
    amount ap_money NOT NULL CHECK (amount = round(amount, 2)),
    realm_id text,
    qbo_po_id text,
    qbo_line_id text,
    PRIMARY KEY (invoice_id, line_number),
    FOREIGN KEY (invoice_id, realm_id, qbo_po_id)
        REFERENCES vendor_invoices (invoice_id, realm_id, qbo_po_id),
    FOREIGN KEY (realm_id, qbo_po_id, qbo_line_id)
        REFERENCES qbo_purchase_order_lines (realm_id, qbo_po_id, qbo_line_id) MATCH FULL
);
CREATE INDEX invoice_items_po_line_idx ON invoice_line_items (realm_id, qbo_po_id, qbo_line_id)
    WHERE qbo_line_id IS NOT NULL;

CREATE TABLE qbo_tokens (
    realm_id text PRIMARY KEY CHECK (realm_id ~ '^[0-9]+$'),
    access_token_ciphertext bytea NOT NULL CHECK (octet_length(access_token_ciphertext) > 0),
    refresh_token_ciphertext bytea NOT NULL CHECK (octet_length(refresh_token_ciphertext) > 0),
    access_expires_at timestamptz NOT NULL,
    refresh_expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
