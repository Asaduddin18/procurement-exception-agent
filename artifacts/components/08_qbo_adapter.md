# QuickBooks Online Adapter

The AP adapter imports purchase orders into the receipt ledger. It posts bills only for invoices with a durable quantity reservation, an approved procurement run, an `AP_INVOICE_APPROVED` audit event, and the original PDF. The AP policy and review workflow will write that approval in a later phase. The operator CLI and the backend service use the same posting gate.

```mermaid
sequenceDiagram
    participant AP as AP adapter
    participant DB as PostgreSQL
    participant QBO as QuickBooks
    AP->>DB: Load approval, reservation and PDF
    AP->>QBO: Check vendor and document number
    AP->>DB: Claim bill attempt
    AP->>QBO: Create bill with PO line links
    AP->>DB: Persist confirmed bill ID
    AP->>QBO: Attach original PDF to bill
    AP->>DB: Persist confirmed attachment ID
```

## Implemented behavior

| Operation | Contract |
| --- | --- |
| OAuth authorization | The operator CLI opens a loopback callback with a random state and checks the returned realm. It exchanges the code for tokens and stores them encrypted in PostgreSQL. |
| OAuth refresh | A PostgreSQL transaction lock serializes refresh per realm. The adapter persists the latest access and refresh tokens together. An API 401 can trigger one refresh and retry. An expired or revoked refresh grant requires reconnection. |
| PO sync | Paginate QuickBooks POs. Resolve vendor display names exactly to the internal vendor catalog, and source item SKUs exactly within that vendor and currency. Keep QuickBooks PO, vendor, item and line IDs. Mark removed lines inactive and missing POs closed without removing historical receipt links. Commit only after the entire scan succeeds. |
| Bill export | Use the approved invoice number, currency and exact decimal amounts. Each item expense line includes its QuickBooks item and native PO line reference. Freight requires a configured expense account. The private note contains the invoice ID, approval audit ID, PO number and optional approval notes. |
| PDF export | Store the original intake bytes on the confirmed Bill via the multipart Upload API. Persist the Bill ID first so subsequent attachment runs cannot recreate the bill. |

Bill amounts must reconcile to item amounts plus approved freight. The current payload has no tax allocation. A total with an unexplained difference is held before the network write.

## Recovery

- Reads retry bounded transient failures with backoff. Each write carries a stable QuickBooks request ID and retries explicit throttling only within the short backoff window. An exhausted Bill throttle returns the invoice to `RESERVED` for a later retry.
- If a bill response is lost, the posting call leaves the invoice in `POST_UNKNOWN`. A replay queries QuickBooks by document number and checks the vendor, total, and unique invoice audit marker before recording the Bill ID. If no matching bill can be confirmed, the reservation stays in place for operator reconciliation.
- A known rejected attachment write can be retried against the same confirmed Bill. If the upload outcome is ambiguous, a replay reads the attachables by deterministic filename and verifies the Bill reference. It keeps the billing record even when the attachment is unresolved.
- Failures are reported without token values or raw accounting payloads. Keep the Fernet key separate from the database backup and retain it across process restarts.

## Operator runbook

1. Start PostgreSQL and apply migrations. Export `DATABASE_URL`, `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REALM_ID`, `QBO_REDIRECT_URI`, and `QBO_TOKEN_KEY` into the operator process. Use the Intuit development credentials for the sandbox. Register the same loopback URI in Intuit, for example `http://localhost:8765/qbo/callback`. QuickBooks defaults to the sandbox host. Configure `QBO_FREIGHT_ACCOUNT_ID` when approved freight will be posted.
2. Run `python -m backend.qbo_cli connect`. Open the displayed Intuit URL and approve the expected sandbox realm.
3. Run `python -m backend.qbo_cli sync`. Verify the imported PO lines by QuickBooks ID. For an invoice with the required approval and reservation, run `python -m backend.qbo_cli post <invoice-uuid>`. The command prints the confirmed Bill and attachment IDs.
4. In QuickBooks, verify the bill document number, attachment, private audit note and linked PO line. Replay the same command and verify it returns the same IDs.

```bash
docker compose up -d --wait postgres
python -m seed.seed --migrate-only
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q backend/tests/test_qbo_adapter.py backend/tests/test_ap_store.py
```

In the US Plus sandbox, the live check imported both Apex reference POs and posted a separate synthetic USD 120.00 partial bill. QuickBooks returned the native PO line link, the attached PDF and the private audit note. Replay returned the same IDs, and the source PO remained open.

The automated suite exercises OAuth exchange and refresh with simulated Intuit responses and the actual PostgreSQL ledger. It also covers pagination, removed PO lines, the approval gate, confirmed bill replay, and response loss during the bill and attachment writes.

[Intuit OAuth 2.0](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0), [Accounting API request IDs](https://developer.intuit.com/app/developer/qbo/docs/learn/learn-basic-field-definitions#request-id), [Intuit attachments](https://static.developer.intuit.com/output_html/qbo/docs/workflows/attach-images-and-notes.html).
