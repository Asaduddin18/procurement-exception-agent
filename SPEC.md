# Build specification

## Backend, MCP, and fixtures

- Use Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy with psycopg 3, PostgreSQL 16, and LangGraph with PostgresSaver. Parse multi-page PDFs using PyMuPDF or Docling and extract structured data with OpenAI or Anthropic. Make Langfuse tracing optional.
- Accept PDF uploads or email webhook payloads at `POST /api/v1/ingest`. Enforce a unique SHA-256 of the original PDF bytes in `processed_events` and retain the source ID as provenance. Commit the input, run, and durable job together. Return HTTP 200 with `duplicate_ignored` and the original run ID for duplicates. Return HTTP 202 with a run ID and thread ID for a new queued run, then process it asynchronously.
- Store vendors, price catalog, runs, audit logs, and graph checkpoints in Postgres.
- Expose `get_vendor_contract_terms`, `get_approved_price_catalog`, and `search_sop_policy` via FastMCP over stdio and HTTP. Use exact PostgreSQL lookups for vendor terms and catalog prices. For SOPs, filter SQL candidates by active status, category, region, and effective date before pgvector ranks them. Return policy version and citations with the results.
- Extract vendor, PO number, payment terms, SKU, description, quantity, unit price, line total, and grand total. Fetch contract terms, catalog prices, and scoped SOP evidence from MCP. Load approved, versioned rule values by exact SQL. Reconcile extracted totals and calculate price variance, dollar overcharge, and threshold checks in deterministic Python.
- Flag unit prices above the allowed variance (5% in the demo), payment terms that conflict with the applicable contract or policy, and totals over $10,000 requiring Finance Director sign-off. Include exact amounts and policy citations in exceptions.
- Auto-approve and audit clean runs. Persist exceptional runs as awaiting approval and interrupt the graph with the exception payload. Resume the checkpoint from `POST /api/v1/runs/{run_id}/resolve` with APPROVE, DISPUTE, or REJECT and reviewer notes. Audit the resolution. For disputes, draft a vendor email with the exact SKU, quoted and contracted prices, and citations.
- Seed Apex Industrial Supply (Net-30, SS-316 Valve at $120.00) and two additional vendors. Generate `PO-2026-419_Apex_Overcharge.pdf`, a four-page $18,450 Net-15 PO with a $145.00 valve on line 3. Generate `PO-2026-420_Clean.pdf`, a $4,200 Net-30 PO that matches the catalog.
- Provide Docker Compose startup and an environment example. Verify duplicate ingestion, clean auto-approval, MCP lookups, and checkpoint pause/resume with end-to-end pytest before building the UI.

See the [system architecture](artifacts/01_system_architecture.md) and [architecture decisions](artifacts/02_architecture_decisions.md) for component contracts and failure handling.

## Control tower

- Use Next.js App Router, TypeScript, and Tailwind CSS for a dark-mode interface.
- Show runs with ID, PO number, vendor, total, status, and duplicate badge.
- Provide a split view with the extracted PO and flagged line items alongside contract and SOP findings. Include reviewer notes and actions to approve, draft a dispute, or reject.
- Show node timestamps, latency, available model costs or a Langfuse trace link, and a duplicate upload test action.
