# Invoice Workflow and AP Control Tower

## Purpose & Scope

Invoice uploads use the existing durable intake API with `document_type=INVOICE`. A separate graph evaluates the source PDF against the AP ledger. Clean invoices reserve quantities and publish approval plus a durable posting job. Price and freight exceptions pause at a PostgreSQL LangGraph checkpoint. Reviewer decisions resume that checkpoint through the existing authenticated resolve endpoint.

The Control Tower supports PO and invoice uploads, invoice line highlights, and a comparison of ordered, received, allocated and billed quantities. It shows confirmed Bill and attachment IDs and exposes authenticated retry for exhausted posting jobs.

```mermaid
flowchart TD
    API["FastAPI durable intake"] --> R{"Document type"}
    R -->|PO| PO["Existing PO graph"]
    R -->|Invoice| AP["Extract and match AP evidence"]
    AP --> G{"Findings"}
    G -->|Exceptions| HITL["PostgresSaver review interrupt"]
    G -->|Clean| Q["Recheck and reserve received quantity"]
    HITL -->|Approve| Q
    HITL -->|Dispute or reject| A["Persist reviewer outcome"]
    Q --> DB["Atomic approval and posting job"]
    DB --> W["QuickBooks posting worker"]
    W --> OK["Bill and PDF confirmed"]
```

## Data Contracts / Schemas

- `IntakeMetadata.document_type` accepts `PO` or `INVOICE` and defaults to `PO`. Invoice uploads queue an `EVALUATE` job. PO uploads retain their extraction and policy jobs.
- `GET /api/v1/runs/{run_id}` adds an `ap` field containing extracted invoice evidence, a comparison snapshot, and the posting record. Run summaries expose the document type.
- `POST /api/v1/runs/{run_id}/resolve` accepts the existing APPROVE, DISPUTE and REJECT contracts and reviewer bearer credentials.
- `QBO_POST` jobs consume approved invoices. A run becomes `SYNCED_TO_QBO` only after Bill and attachment confirmation. Until then, it remains approved with its posting state visible.
- `POST /api/v1/runs/{run_id}/retry-sync` requires reviewer authentication and requeues only an exhausted posting job on an approved run.

## Invariants & Edge Cases

Statements, duplicate invoices, missing or ambiguous evidence, unit and currency mismatches, unsupported tax or discount, and excess quantities block approval. Price and freight exceptions can be approved. The default Finance Director cap is USD 10,000. Approval reruns exact matching and accepts only the same reviewable discrepancies shown to the reviewer. A new discrepancy or insufficient current capacity fails the run before the posting handoff.

Reservation uses the Phase 2 transaction locks. Approval projection and QBO job creation commit together. A replayed checkpoint can recreate the projection without creating a second posting job. The posting worker holds a session lock per invoice, fences completion by its lease, and uses the adapter's persisted Bill and attachment IDs for recovery.

Dispute drafts cite the invoice, PO, SKU, line number, compared amounts and source run. They are deterministic drafts from verified evidence. The reviewer copies the text for vendor communication.

Langfuse remains optional and records stage metadata. Its credentials and all QBO credentials are supplied through environment configuration.

## Verification Commands

```bash
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q backend/tests/test_ap_workflow.py backend/tests/test_qbo_adapter.py
npm --prefix frontend run typecheck
npm --prefix frontend run test:e2e
```

The workflow suite verifies clean approval and posting queue, duplicate intake, checkpoint restart, repeated resolutions, rejection, dispute references, statement and quantity blocks, changed PO evidence and authenticated posting recovery. The browser suite uploads an invoice, inspects the comparison, drafts a dispute, and confirms statement approval is disabled.
