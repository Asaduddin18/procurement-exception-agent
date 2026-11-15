# MCP server and scoped SOP retrieval

## Purpose and scope

FastMCP gives the procurement workflow three read-only operations for gathering contract evidence, catalog prices, and relevant SOP passages. Phase 4 will use typed rules and exact prices to decide whether a PO can be approved. Retrieval supplies source evidence for exceptions and reviewer decisions.

```mermaid
flowchart TD
    C["Procurement workflow or MCP client"] --> M["FastMCP tool contracts"]
    M -->|Vendor or SKU identifiers| X["Exact SQL"]
    M -->|SOP query| Q["Embedding provider"]
    Q --> S["Scoped PostgreSQL candidate set"]
    S --> V["Exact pgvector cosine ranking"]
    X --> F["Vendor and catalog facts"]
    V --> P["Versioned SOP citations"]
```

## Data contracts and schemas

FastMCP exposes the same input and Pydantic v2 output schemas on stdio and Streamable HTTP. Clients read the payload from `structuredContent`.

| Tool | Inputs and normalization | Result |
| --- | --- | --- |
| `get_vendor_contract_terms(vendor_name)` | One display name, trimmed, up to 200 characters. The lookup is case-sensitive. | `found` with ID, name, payment days, and contract reference, or `vendor_not_found` with a null vendor. |
| `get_approved_price_catalog(vendor_name, sku_list)` | One resolved vendor and 1–100 case-sensitive SKUs, each trimmed and bounded to 120 characters. Repeat SKUs collapse in first-request order. | `found`, `partial`, `sku_not_found`, or `vendor_not_found`. Each price includes description, unit, currency, and an exact decimal serialized as a JSON string. `missing_skus` identifies every requested key without a result. |
| `search_sop_policy(query, category=None, region="US")` | Nonblank query up to 1,000 characters. Category defaults to `procurement` and is normalized to lowercase. Region is an uppercase two-letter code. The service takes the evaluation date from a UTC clock. | `found` or `no_matches` with evaluated scope and date. Each citation includes policy ID, version, status, effective date, category, region, chunk number, source location, text, and cosine distance. The result contains up to five passages. |

### Vendor and catalog path

The repository binds the vendor display name to an equality query against its unique database key. It uses the returned vendor ID and `sku = ANY(...)` against the composite catalog key. A read-only, repeatable-read transaction keeps the vendor and catalog queries on one snapshot. The response restores request order because SQL row order is unspecified. An unknown vendor stays distinguishable from a known vendor with a missing SKU. In the seed data, the same SS-316 Valve maps to USD 120 for Apex and USD 128 for Harbor.

### SOP retrieval path

1. Normalize request scope and capture the UTC date once. The caller supplies a known region and category from trusted run metadata on the decision path.
2. Encode the query in the configured embedding space, then validate that the vector has 1,536 finite elements and nonzero magnitude.
3. Join a passage to its policy metadata by the composite policy ID and version. Materialize only rows with active status, exact category and region, `effective_date <= evaluated_on`, and a matching embedding space. The query binds all caller-provided values.
4. Order eligible rows by cosine distance, then policy ID, descending version, and chunk number for stable ties. Apply the five-row limit after ranking. A smaller distance means a closer match. A ranking score carries no approval authority.

```sql
WITH eligible AS MATERIALIZED (
    SELECT policy_id, version, chunk_number, embedding
    FROM public.sop_policy_versions
    JOIN public.sop_policy_chunks USING (policy_id, version)
    WHERE status = 'ACTIVE'
      AND category = :category
      AND region = :region
      AND effective_date <= :evaluated_on
      AND embedding_space = :embedding_space
)
SELECT policy_id, version, chunk_number,
       embedding <=> :query_vector AS cosine_distance
FROM eligible
ORDER BY cosine_distance, policy_id, version DESC, chunk_number
LIMIT 5;
```

The materialized CTE creates an explicit metadata boundary. An unscoped top-five query followed by filtering can return zero usable rows even when the relevant policy exists. The integration test creates just that scenario and confirms the scoped search returns the active approval passage. The current implementation uses exact cosine ranking on the scoped set. The partial index on active policy metadata supports candidate selection. Evaluate candidate counts and query latency before introducing an approximate vector index.

Multiple active, eligible versions may appear in the result. The tool returns their identities. Approval code must resolve the applicable approved rule version by exact SQL and use matching citations. The MCP database role has no access to the rules table.

### Embedding providers

- **Fixture** uses a deterministic projection onto the seed's three topic dimensions for terms, approval, and variance. It runs offline and is purpose-built for the synthetic SOP passages. The negative controls are deliberately closer than the relevant active approval passage.
- **OpenAI** uses `text-embedding-3-small` with 1,536 dimensions under `openai:text-embedding-3-small:1536`. It sends the query text and embedding configuration to the embeddings endpoint over `httpx.AsyncClient`. Enable it with `MCP_EMBEDDING_PROVIDER=openai` and `OPENAI_API_KEY` after indexing the deployment's SOP corpus in that space. The eligible set will be empty if the database contains only fixture vectors. The application should scope the query to the policy question and keep customer documents and commercial line items out of the query.
- The OpenAI call uses an eight-second HTTP timeout and up to three attempts with exponential waits for HTTP `429` and `5xx`. Invalid payloads, timeouts, or other errors surface as tool failures. The test suite uses a mock endpoint response to verify retries. It requires no API key.

### Transport and database boundaries

The default stdio process is started by its MCP client. Protocol output is reserved for MCP messages. For Streamable HTTP the entry point binds to loopback on port 8001, serves `/mcp`, and uses stateless JSON responses. A deployment exposing this service outside loopback must supply a trusted service identity and ingress policy. Region and category parameters scope retrieval.

Migration `002_mcp_read_access.sql` creates a `NOLOGIN` role with `SELECT` on vendors, catalog, SOP versions, and SOP chunks. Provision a dedicated login in that role. Each call opens a short-lived async PostgreSQL connection with a three-second connect timeout, a five-second statement timeout, and a read-only transaction. Database privileges enforce the access boundary even if a caller bypasses the application's transaction setup. The CLI requires a separate `MCP_DATABASE_URL`.

## Invariants and edge cases

| Condition | Behavior and verification |
| --- | --- |
| Archived, future-effective, wrong-region, wrong-category, or wrong-space match is closer than the active SOP | The SQL candidate filter excludes it before ranking. Separate controls isolate region and category. A same-day policy is eligible. |
| Scoped query genuinely has no eligible passages | The tool returns `no_matches` with an empty citations array. |
| Database is unavailable or the login lacks privileges | The MCP call fails. The application logs SQLSTATE and returns a safe failure message. A caller can distinguish it from an empty search. |
| Provider emits a malformed embedding | Validation fails before the vector reaches PostgreSQL. |
| Price lookup gets a similarly spelled SKU or a vendor alias | The unmatched identifier is explicit. Exact prices stay tied to the requested catalog. |
| Read-only identity attempts to mutate reference data or read intake PDFs | PostgreSQL rejects it. Integration tests connect with an actual login in the read-only role. |
| Caller supplies invalid tool arguments | FastMCP returns a protocol tool error. Both transport tests exercise validation and all three tools. |

## Verification commands

From the repository root, with Docker Compose and Python 3.11+:

```bash
docker compose up -d --wait postgres
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r seed/requirements-test.txt -r mcp_server/requirements.txt
python -m seed.seed
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q seed/tests mcp_server/tests
```

The suite creates disposable databases and logins. Its owner DSN must allow database creation, role creation, and installation of the vector extension. The local Compose owner meets those prerequisites. Integration tests skip when `TEST_DATABASE_URL` is unset. CI supplies the DSN and runs the complete suite.

To provision a local MCP login once, open psql and assign a password interactively:

```bash
docker compose exec postgres psql -U procurement -d procurement
```

```sql
CREATE ROLE procurement_mcp LOGIN IN ROLE procurement_mcp_reader;
\password procurement_mcp
\q
```

Export a DSN using that password, then start the HTTP server in one terminal. The same entry point uses stdio when started without `--transport`.

```bash
export MCP_DATABASE_URL='postgresql://procurement_mcp:YOUR_PASSWORD@127.0.0.1:5432/procurement'
export MCP_EMBEDDING_PROVIDER=fixture
python -m mcp_server.server --transport streamable-http --port 8001
```

In a second terminal, activate the same virtual environment and call the server with the MCP SDK:

```bash
python - <<'PY'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://127.0.0.1:8001/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "search_sop_policy", {"query": "Finance Director approval cap", "region": "US"}
            )
            assert not result.isError
            print(result.structuredContent)

asyncio.run(main())
PY
```

Use the fixed-date SQL check below to inspect the metadata boundary independently of the current server clock. At that date, the seeded result is `US-PROCUREMENT` version `2026` with three candidate chunks.

```bash
docker compose exec -T postgres psql -U procurement -d procurement -v ON_ERROR_STOP=1 <<'SQL'
SET ROLE procurement_mcp_reader;
SELECT policy_id, version, count(*) AS eligible_chunks
FROM public.sop_policy_versions
JOIN public.sop_policy_chunks USING (policy_id, version)
WHERE status = 'ACTIVE'
  AND category = 'procurement'
  AND region = 'US'
  AND effective_date <= DATE '2026-10-19'
  AND embedding_space = 'procurement-fixture-v1'
GROUP BY policy_id, version;
SQL
```

## References

- [MCP Python SDK v1 documentation](https://py.sdk.modelcontextprotocol.io/v1/)
- [PostgreSQL 16 CTE materialization](https://www.postgresql.org/docs/16/queries-with.html#QUERIES-WITH-CTE-MATERIALIZATION)
- [pgvector distance operators and filtering](https://github.com/pgvector/pgvector)
