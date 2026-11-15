# Async intake and validated PO extraction

## Purpose and scope

Phase 3 accepts a procurement PDF, persists it with a durable job, and exposes the resulting typed purchase order or a failure code. The worker stops at the `EXTRACTED` state. Phase 4 will consume the validated record for contract verification, exception handling, and approval decisions.

## Architecture and design

```mermaid
sequenceDiagram
    participant C as Upload client
    participant A as FastAPI
    participant D as PostgreSQL
    participant W as Extraction worker
    participant G as DeepSeek

    C->>A: POST PDF bytes or email attachment JSON
    A->>D: Register SHA-256 and run in one transaction
    alt Hash already registered
        D-->>A: Return original run and thread IDs
        A-->>C: 200 duplicate_ignored
    else New PDF
        A->>D: Persist PDF, PROCESS job, and intake audit
        D-->>A: Commit
        A-->>C: 202 queued
        W->>D: Claim job and load original bytes
        W->>W: Parse each page in a subprocess
        W->>G: Send page-tagged text for JSON extraction
        G-->>W: Extracted fields
        W->>W: Reconcile printed totals with Decimal
        W->>D: Finish only if this worker still owns the lease
    end
```

**API and idempotency.** `backend/api.py` limits request size before parsing. The HTTP handler awaits only the database transaction for intake. `backend/store.py` computes SHA-256 over the decoded PDF bytes. It creates a provisional run and attempts to insert the digest using `ON CONFLICT (sha256) DO NOTHING`. Under concurrency, the unique key resolves ownership. A duplicate removes its provisional run in the same transaction and reads the committed original using PostgreSQL's read-committed isolation. Only the winning request stores input, enqueues the job, and writes the intake audit. The API sends the response after that transaction closes. An API restart or lost response leaves a committed job available to the worker.

**Worker lease.** The worker claims one ready or expired `PROCESS` job with `FOR UPDATE SKIP LOCKED`, increments its attempt count, and commits a 120-second lease with a new UUID. It processes the job outside the database transaction. Another worker can reclaim the input after expiration. Completion matches both the lease token and expiry, then commits the job, run projection, and audit event together. A stale worker can no longer change the run. Three extraction claims are allowed. The next reclaim after three expired leases records terminal exhaustion without another provider call. The per-job processing deadline is 90 seconds.

**PDF parser.** The worker starts `backend.pdf_worker` as a subprocess that reads bytes on stdin, opens the PDF with PyMuPDF, and emits text in page order. The model receives page numbers and text. The parser has a 20-second timeout, a 32-page limit, and a total limit of 100,000 text characters. It rejects encrypted, corrupt, and textless pages. This release supports native-text PDFs. Scanned documents with no text need a separate OCR path. Isolating parsing keeps native PDF work out of the API process and gives the worker a subprocess it can terminate.

**Provider boundary.** The worker uses LangChain's `ChatOpenAI` client with DeepSeek's OpenAI-compatible Chat Completions endpoint, model `deepseek-flash`, and the DeepSeek credential stored in `OPENAI_API_KEY`. `with_structured_output(ExtractedPurchaseOrder, method="json_mode")` parses generated JSON through Pydantic. The system message includes a concrete JSON example, treats PDF text as untrusted data, and asks the model to copy amounts. The worker sends only page-tagged text to the model. The API process requires no provider key. MCP's optional OpenAI embedding adapter uses `MCP_OPENAI_API_KEY` to keep the credentials separate. `MCP_EMBEDDING_PROVIDER=fixture` continues to support local and CI verification without an embedding key.

**Retry and rejection.** Only provider `429` and `5xx` responses receive up to three HTTP attempts with exponential backoff. Invalid output, unsupported PDFs, provider authentication failures, and exhausted provider retries produce a terminal `FAILED` run with a safe reason code. The worker deadline bounds the overall attempt. HTTP provider tests use an intercepted `httpx` transport and verify the destination, model, request format, authentication header, and response parsing.

## Data contracts and schemas

| Surface | Contract |
| --- | --- |
| Manual intake | `POST /api/v1/ingest` with multipart `file` and optional `source_id`, `category` and `region`. Defaults are `procurement` and `US`. |
| Email webhook | Same endpoint with JSON containing required `source_id` and `pdf_base64`, plus optional scope. A shared `INGEST_WEBHOOK_TOKEN` enables this path. Send it as an HTTP bearer token. |
| Successful intake | `202` returns `status: queued`, `run_id` and `thread_id`. Repeating the same bytes returns `200` with `status: duplicate_ignored` and the original identifiers. The first submission supplies the stored scope and provenance. |
| Run status | `GET /api/v1/runs/{run_id}` returns run and thread IDs, status, scope, an optional extracted PO or error code. Unknown well-formed run IDs return `404`. |
| Extracted PO | Vendor, PO number, currency, printed payment terms, 1–200 line items, freight, tax, discount, printed grand total, and the grand-total page. Each line has a number, SKU, description, unit, positive quantity, unit price, printed line total, and a one-based source page. |
| Persistence | Migration `003_ingestion_extraction.sql` adds `EXTRACTED`, `extracted_po` and `error_code`. The run projection keeps top-level PO number, currency, and grand total queryable. |

The Phase 3 API is intended for loopback or trusted internal use. The manual upload and run-status routes rely on deployment access controls. The email webhook has its own credential.

## Invariants and edge cases

| Condition | Handling |
| --- | --- |
| Concurrent requests contain identical bytes | The unique digest and one database transaction produce one committed payload and job. Renaming or forwarding the PDF still returns the first run. An edited or regenerated PDF can have a different digest. |
| Intake transaction fails midway | All rows roll back. The next upload can register normally. |
| Worker dies or runs beyond lease expiry | The job becomes reclaimable. Token fencing rejects the old worker's outcome. The provider may be called again during recovery, and only one terminal result is committed. |
| Unsupported media type, malformed metadata or encoding, or oversized file | Media type errors return `415`. Invalid inputs return `422` or multipart syntax errors return `400`. Request and PDF size limits return `413`. The decoded PDF limit is 10 MiB. |
| A valid PDF header masks a corrupt document | Intake can queue it. The worker parses the full PDF and records the failure. |
| Printed quantity and price disagree with the printed line total | The Pydantic v2 `@model_validator` computes quantity × unit price with `Decimal` and half-up rounding to two decimal places. The result must match the printed total exactly. |
| Aggregate total is inconsistent or a line number is repeated | The same validator checks the sum of line totals plus freight and tax minus discount. It rejects a mismatched grand total or repeated line number. Fractional values must enter as decimal strings. Unit prices and quantities support four decimal places, while line totals and PO adjustments use two. |
| Page attribution is outside the PDF | The worker rejects the extraction after comparing one-based page references with the parsed page count. Page attribution supports inspection. The validator checks arithmetic and page bounds independently of the model. |

## Verification commands

From the repository root:

```bash
docker compose up -d --wait postgres
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r seed/requirements-test.txt -r mcp_server/requirements.txt -r backend/requirements.txt
python -m seed.seed
export TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement
python -m pytest -q seed/tests mcp_server/tests backend/tests
```

The integration suite creates disposable PostgreSQL databases. It checks concurrent upload and email forwarding, rollback after a failed job insert, stale lease recovery, and extraction state transitions. The model protocol and retry tests use mocked HTTP responses and require no credits. The CI HTTP smoke test also starts Uvicorn and uses curl against the running API.

In one terminal, start the API on loopback:

```bash
python -m uvicorn backend.api:create_app --factory --host 127.0.0.1 --port 8000
```

In another, submit the same PDF twice. A fresh PostgreSQL volume or previously unseen PDF yields `202` and then `200`:

```bash
pdf=seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf
curl -sS -w '\nHTTP %{http_code}\n' -F "file=@$pdf" http://127.0.0.1:8000/api/v1/ingest
curl -sS -w '\nHTTP %{http_code}\n' -F "file=@$pdf" http://127.0.0.1:8000/api/v1/ingest
```

To verify a live extraction, set `OPENAI_API_KEY` to your DeepSeek credential and run `python -m backend.worker --once` from the repo root. It claims the oldest eligible process job. Query its ID from the intake response:

```bash
curl -fsS "http://127.0.0.1:8000/api/v1/runs/$run_id"
```

On a successful extraction the run is `EXTRACTED`. For the Apex overcharge fixture, inspect the four-page acquisition indirectly via line 3's `source_page`, the printed unit price of USD 145.00, and the USD 18,450.00 grand total. The ledger can be inspected with:

```bash
docker compose exec -T postgres psql -U procurement -d procurement <<'SQL'
SELECT r.run_id, r.status AS run_status, j.status AS job_status, j.attempts, r.error_code
FROM procurement_runs r JOIN procurement_jobs j USING (run_id)
ORDER BY r.created_at DESC;
SELECT run_id, event_type FROM audit_logs ORDER BY audit_id;
SQL
```

## References

- [PyMuPDF process isolation guidance](https://pymupdf.readthedocs.io/en/latest/recipes-multiprocessing.html)
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)
- [LangChain structured output](https://reference.langchain.com/python/langchain-openai/chat_models/base/ChatOpenAI/with_structured_output)
