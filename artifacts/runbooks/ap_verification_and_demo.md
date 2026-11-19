# AP Verification and Demo Guide

## Local evaluator walkthrough

Start the stack from the repository root. The initializer applies migrations and loads a clearly labeled offline PO with ten valves ordered and six received.

```bash
docker compose up --build
```

Open http://127.0.0.1:3000. Select **Invoice or statement** in the document selector. The AP fixtures in `seed/sample_pdfs` are recognized in fixture extraction mode.

| File | Expected result |
| --- | --- |
| `AP_DEMO_Clean.pdf` | Two valves at $120, total $240. Approves and reserves two received units. Shows awaiting QuickBooks connection or posting. |
| `AP_DEMO_Price_Exception.pdf` | One valve at $145. Pauses for review with PO and catalog price findings. |
| `AP_DEMO_Quantity_Exception.pdf` | Eight valves. Approval is blocked because the receiving ledger has only six units before other allocations. |
| `AP_DEMO_Statement.pdf` | Statement classification blocks invoice creation and approval. |

Use `demo-procurement-reviewer` to approve the price exception, reject it, or draft a dispute. Notes are required. For orders above the finance cap, use `demo-finance-director`. Each run accepts a single resolution. Uploading the same PDF opens its original run and increments the duplicate badge.

In Audit & Trace, inspect the invoice graph timings, approval and resolution events. The default evaluator retains approved invoices in the posting outbox until a QuickBooks connection and posting worker are configured.

## QuickBooks sandbox walkthrough

Use a separate configured sandbox environment for the live path. Set `QBO_REALM_ID` to the actual sandbox, enable DeepSeek extraction through `PROCUREMENT_EXTRACTION_PROVIDER=deepseek`, and provide `OPENAI_API_KEY` plus the QBO development credentials and durable encryption key from the adapter runbook. Use invoices matching actual synced POs and accepted warehouse receipts in that realm.

Run the adapter operator commands against the same PostgreSQL database and environment as Compose. The callback URI must match the registered local callback.

```bash
python -m backend.qbo_cli connect
python -m backend.qbo_cli sync
docker compose --profile qbo up -d --build
```

Upload a matching invoice. After approval, inspect `SYNCED_TO_QBO`, the Bill ID, source PDF attachment ID, and audit event. In QuickBooks, verify the invoice number, native PO line link, original PDF and audit note. Submit the same PDF again and confirm no second Bill is created.

For exhausted posting jobs, fix the connection first, then use **Retry QuickBooks Sync** with a reviewer token. An uncertain create is reconciled using its document number and audit marker. If the adapter cannot confirm an ambiguous write, an operator must reconcile the record in QuickBooks before retrying.

## Verification commands

```bash
python -m pip install -r seed/requirements-test.txt -r backend/requirements.txt -r mcp_server/requirements.txt
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q seed/tests mcp_server/tests backend/tests
npm --prefix frontend ci
npm --prefix frontend run typecheck
npm --prefix frontend run test:e2e
```

```sql
SELECT run_id, document_type, status, error_code
FROM procurement_runs ORDER BY created_at DESC;

SELECT invoice_number, status, qbo_bill_id
FROM vendor_invoices ORDER BY created_at DESC;

SELECT kind, status, attempts FROM procurement_jobs WHERE kind = 'QBO_POST';

SELECT run_id, event_type, actor_id FROM audit_logs
WHERE event_type IN ('AP_INVOICE_APPROVED', 'SYNCED_TO_QBO', 'QBO_SYNC_RETRY')
ORDER BY audit_id;
```

The automated workflow and adapter tests use real PostgreSQL transactions and checkpoints with simulated model and QBO responses. They cover API approval, durable posting, lost responses and attachment recovery. The connected sandbox requires current credentials for live verification.

## Demo recording sequence

1. Upload the clean invoice and inspect ordered, received and billed quantities.
2. Upload the price exception and inspect the highlighted line and discrepancy.
3. Enter reviewer notes and draft a dispute with the exact PO, SKU and amounts.
4. Upload the statement and show blocked approval.
5. Replay a duplicate PDF and inspect the audit trail.
6. In the connected sandbox environment, show a synced bill and its original PDF in QuickBooks.
