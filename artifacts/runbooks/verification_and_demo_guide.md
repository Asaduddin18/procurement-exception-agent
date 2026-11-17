# Verification and demo guide

## Startup and configuration

From the repository root with Docker Engine and Compose v2:

```bash
docker compose up
```

The first run builds the images. Open the [Control Tower](http://127.0.0.1:3000) once its proxy can reach the API. For detached startup and inspection:

```bash
docker compose up -d --build
docker compose ps -a
curl -fsS http://127.0.0.1:3000/api/procurement/runs
```

PostgreSQL starts first. The initializer applies checksum-tracked migrations, loads the synthetic catalog and policies, and provisions the read-only MCP login. It exits successfully before FastAPI, the extraction worker, and MCP start. The review worker waits for MCP and opens its checkpoint saver. Next.js waits for FastAPI's database-backed health check. The database, UI, and API host ports bind to loopback. MCP uses the internal Compose network.

The defaults are a local evaluator setup with synthetic data and demo credentials. To override them, copy `.env.example` to `.env`. The default fixture path recognizes only the committed sample bytes by SHA-256, runs the PDF parser, and uses deterministic structured extraction. It then traverses the actual PostgreSQL, MCP, workflow, and browser paths. `DEMO_POLICY_AS_OF=2026-10-19` fixes the MCP and review clocks to the same policy date so the demo remains repeatable.

For live extraction, set `PROCUREMENT_EXTRACTION_PROVIDER=deepseek` with a DeepSeek API key in `OPENAI_API_KEY`. Set `DEMO_POLICY_AS_OF=` to use the current UTC evaluation date and provision approved policy and catalog evidence for the documents being evaluated. Set `REVIEWER_CREDENTIALS_JSON` to override the two demo credentials. Langfuse is optional. Set its keys and project ID to enable the trace link.

## Evaluator walkthrough

| Step | Action | Evidence to inspect |
| --- | --- | --- |
| 1 | Upload `seed/sample_pdfs/PO-2026-420_Clean.pdf`. | Queue shows `PO-2026-420`, $4,200, and **Approved**. Audit records intake, extraction, and approval. |
| 2 | Upload `seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf`. | Queue shows **Awaiting approval**. Inspector flags $145 quoted versus $120 contracted for SS-316 Valve, $1,250 overcharge, 20.83% variance, Net-15 versus Net-30, and the Finance Director rule. Expand a policy citation to see the source. |
| 3 | In **Audit & Trace**, click **Test Duplicate Upload**. | The original run ID stays selected, the duplicate count increases, and the queue still contains two runs on a fresh volume. |
| 4 | Enter review notes and `demo-procurement-reviewer`. Click **Approve Exception**. | The API rejects the action because Finance Director approval is required. The run remains paused. |
| 5 | With the same reviewer, select **Draft Dispute Email**. | The run becomes **Disputed**, the draft cites the contracted price and policy, and the resolution appears once in the audit. To test **Approve Exception** or **Reject PO** instead, choose that action while the run is paused. Finance Director token is `demo-finance-director`. |

## API and test evidence

Repeat the same upload to verify the original receipt and run identity. On a fresh volume the first response is HTTP `202`; the second is HTTP `200 duplicate_ignored`.

```bash
api=http://127.0.0.1:8000/api/v1
pdf=seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf
curl -sS -w '\nHTTP %{http_code}\n' -F "file=@$pdf" "$api/ingest"
curl -sS -w '\nHTTP %{http_code}\n' -F "file=@$pdf" "$api/ingest"
# Set run_id to the ID in either response.
curl -fsS "$api/runs/$run_id"
curl -fsS "$api/runs/$run_id/audit"
```

Run the backend suite against the running PostgreSQL service using Python 3.11+:

```bash
python -m pip install -r seed/requirements-test.txt -r mcp_server/requirements.txt -r backend/requirements.txt
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q seed/tests mcp_server/tests backend/tests
```

The backend integration suite covers duplicate ingestion, replay-safe approval and rejection, dispute drafts, and checkpoint recovery across a worker restart. The browser workflow starts the evaluator stack from `.env.example` and verifies both seeded orders through the actual application. To run it locally against a fresh default stack with Node 22+:

```bash
npm --prefix frontend ci
npm exec --prefix frontend -- playwright install chromium
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3000 npm --prefix frontend run test:e2e
```

## Inspect and shut down

| Observation | Next check |
| --- | --- |
| Port already in use | Set `FRONTEND_PORT`, `API_PORT`, or `POSTGRES_PORT` in `.env` and use the corresponding URL. Container-to-container ports stay unchanged. |
| API or UI does not become healthy | Inspect `docker compose logs initialize api frontend`. A failed initializer blocks its dependents. |
| A run stays queued or extracted | Inspect `docker compose logs extraction review mcp` and the run's audit trail. An unrecognized PDF in demo mode records `demo_pdf_not_bundled`. |
| Demo volume already contains the fixture | Reupload returns the existing run. Use a fresh volume if the walkthrough needs another terminal action. |

`docker compose down` stops the stack and preserves the database volume. `docker compose down -v` intentionally erases local demo data. Changing the Postgres owner password in `.env` affects database initialization on a fresh volume.
