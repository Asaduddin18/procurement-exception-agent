# Policy escalation and tiered approval

## Purpose and scope

Phase 4 consumes the validated purchase order from Phase 3, evaluates it against approved procurement evidence, and records an approval or requests a human decision. The API publishes the risk findings and the audit trail for the Phase 5 Control Tower.

```mermaid
flowchart TD
    P["Phase 3 persists EXTRACTED and queues EVALUATE"] --> E["Revalidate the persisted PO"]
    E --> M["Fetch exact MCP facts and scoped citations"]
    M --> G["Load approved rules by SQL and evaluate with Decimal"]
    G -->|Clean| A["Checkpoint APPROVED"]
    G -->|Findings| I["Checkpoint interrupt and publish AWAITING_REVIEW"]
    I --> Q["Authorize and queue one resolution"]
    Q --> C["Resume the same thread with Command"]
    C --> T["Checkpoint approval, rejection, or dispute draft"]
    A --> L["Project outcome and audit in PostgreSQL"]
    T --> L
```

## Architecture

**Extraction handoff.** The intake worker inserts an `EVALUATE` job in the same transaction that writes `EXTRACTED`. Migration 004 enqueues evaluation for older runs already in that state. The graph's `extract_document` node revalidates the persisted PO and its decimal totals. It reuses Phase 3 extraction output, so workflow replay avoids a second PDF model call.

**Evidence.** `verify_with_mcp` explicitly calls the vendor, catalog, and SOP tools over Streamable HTTP. Vendor and SKU prices remain exact SQL facts exposed through MCP. SOP retrieval filters active status, effective date, category, region, and embedding space before ranking. The backend independently selects the latest effective active version of each policy ID for the UTC evaluation date and scope. More than one eligible policy ID is ambiguous and blocks approval. Approval amounts, terms, and variance fractions come from the approved SQL rule rows. Each rule references an exact chunk on the same policy and version. The collector tries the SOP lookup twice when required citations and approved rules disagree or are incomplete.

**Risk Gate.** The gate uses Python `Decimal`. It checks compatible currencies and units, payment terms against the vendor contract and policy, exact catalog coverage, and the Finance Director cap. Price variance triggers only when the positive price difference exceeds catalog price × approved variance fraction. The display percentage and overcharge are rounded after comparison. Grand totals strictly above the cap require Finance Director approval. Missing, conflicting, or incompatible evidence blocks both automatic and reviewer approval. Findings identify the observed and expected values, source citations, and SKU and line number where applicable.

**Review and replay.** The asynchronous PostgreSQL LangGraph saver persists `ProcurementState` under the existing thread ID. A review job claims a 120-second lease and holds a PostgreSQL session advisory lock for that thread while it advances or resumes the graph. The API publishes `AWAITING_REVIEW` after the interrupt checkpoint exists. It writes one resolution intent and enqueues `RESUME` transactionally. The worker uses `Command(resume=...)`. If it restarts after saving an interrupt or terminal checkpoint and before updating the run, the next claim projects the saved state. A matching lease token and expiry fence the projection. The graph does not send email. A dispute draft uses a deterministic template backed by the persisted contract and policy evidence.

**Tracing and audit.** PostgreSQL audit events are available for every run. Both Langfuse keys enable optional job, evidence, and risk-gate spans linked by a trace ID derived from the run UUID. Telemetry contains stage names, timing, and run IDs. Commercial data, PDF text, notes, model credentials, and draft text stay out of those spans. Telemetry initialization and export are best effort.

## Contracts

| Surface | Contract |
| --- | --- |
| `GET /api/v1/runs/{run_id}` | Returns the validated PO and `decision_evidence` once evaluated. This includes status, findings, approval tier, whether approval is blocked, the accepted resolution when applied, and any draft. |
| `POST /api/v1/runs/{run_id}/resolve` | Request body has `action` and nonblank `notes`. Authenticate with a bearer token mapped to an actor and role in deployment configuration. Returns `202 resolution_queued`. Repeat with the same actor, role, action, and notes for an idempotent response. |
| `GET /api/v1/runs/{run_id}/audit` | Returns chronological events with timestamps, actors, and structured details. Unknown runs return `404`. Run and audit reads inherit the internal deployment boundary. |
| `APPROVE` | Transitions to `APPROVED`. A finance-tier run requires the configured Finance Director identity. |
| `DISPUTE` | Transitions to `DISPUTED` and returns the draft in the run projection for inspection. |
| `REJECT` | Transitions to `REJECTED` and audits the resolution. |

## Invariants and failure behavior

| Condition | Behavior |
| --- | --- |
| Duplicate PDF arriving through either intake path | The original run and thread are returned. There is one process job and at most one evaluation job per run. |
| Exact cap or exact variance threshold | The boundary is inclusive for the PO. The gate flags only values strictly greater than the threshold. |
| Expired, archived, future, or out-of-scope citation | MCP and backend evidence checks exclude it. Citations must match the rule's policy ID, version, and chunk. |
| Conflicting reviewer actions | A row lock and unique resolution intent allow one action. Different requests after acceptance return `409`. |
| Invalid bearer token or insufficient approval authority | `401` or `403`. Review remains paused. Caller-supplied roles fail request validation. |
| Missing policy, inconsistent evidence, or MCP outage | Review is surfaced and approval is blocked. The audit trail preserves the evidence failure. |
| Worker stops during evaluation or resume | The lease becomes eligible for recovery. A durable outcome or interrupt is projected before considering re-execution. |
| Durable workflow errors persist | The worker retries up to the configured job attempt limit and records a projection failure. |

## Verification and Phase 5 handoff

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

The review tests exercise real PostgreSQL, real LangGraph interrupts and resume, real MCP HTTP, duplicate intake, and all three resolution actions. They inject structured extraction results, so the suite runs without provider credentials. They simulate worker restart after the interrupt checkpoint and after the resumed terminal checkpoint.

For a manual run, provision the reader as described in the [MCP component spec](02_mcp_server_and_rag.md). Start these four processes in separate shells with the environment needed by each:

```bash
MCP_EMBEDDING_PROVIDER=fixture python -m mcp_server.server --transport streamable-http --port 8001
python -m uvicorn backend.api:create_app --factory --host 127.0.0.1 --port 8000
python -m backend.worker
python -m backend.review_worker
```

Set `MCP_DATABASE_URL` for the MCP process. Set `OPENAI_API_KEY` to your DeepSeek key for the extraction worker. For the API, provision distinct bearer credentials by mapping actor IDs to objects with keys `role` and `token` in `REVIEWER_CREDENTIALS_JSON`. Supported roles are `PROCUREMENT_REVIEWER` and `FINANCE_DIRECTOR`.

```bash
api=http://127.0.0.1:8000/api/v1
curl -fsS -F file=@seed/sample_pdfs/PO-2026-420_Clean.pdf "$api/ingest"
curl -fsS -F file=@seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf "$api/ingest"
# Set run_id to the overcharge run from the second response.
curl -fsS "$api/runs/$run_id"
curl -fsS -X POST "$api/runs/$run_id/resolve" \
  -H "Authorization: Bearer $REVIEWER_TOKEN" -H 'Content-Type: application/json' \
  --data '{"action":"DISPUTE","notes":"Verified quoted unit price and Net-15 against contract."}'
curl -fsS "$api/runs/$run_id/audit"
curl -fsS "$api/runs/$run_id"
```

Expect the $4,200 clean fixture to reach `APPROVED`. The Apex exception fixture reaches `AWAITING_REVIEW` with the $1,250 overcharge, 20.83% variance, payment mismatch, and Finance Director requirement. The Phase 5 inspector can display `decision_evidence.decision.findings` alongside the extracted PO and use the audit endpoint for the timeline. Poll the run after a `202` response until the resolution job projects a terminal state.
