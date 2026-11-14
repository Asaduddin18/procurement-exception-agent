# Autonomous B2B Procurement & Contract Exception Agent

A portfolio project for reviewing purchase orders against vendor contracts, price catalogs, and procurement policy. Clean orders will be auto-approved. Exceptions will pause the workflow for a reviewer, who can approve, reject, or draft a vendor dispute.

## Repository layout

| Path | Planned component |
| --- | --- |
| `backend/` | FastAPI ingestion, PostgreSQL persistence, LangGraph workflow, and integration tests |
| `mcp_server/` | FastMCP tools for contract terms, prices, and approval policy |
| `frontend/` | Next.js control tower for exceptions, decisions, and audit history |
| `seed/` | Vendor fixtures, SOP, and sample purchase order PDFs |

See [SPEC.md](SPEC.md) for the workflow, demo fixtures, and acceptance criteria.
