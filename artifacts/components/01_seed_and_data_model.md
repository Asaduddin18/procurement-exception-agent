# Seed and data model

## Purpose and scope

Phase 1 supplies PostgreSQL 16 with pgvector, versioned schema migrations, repeatable synthetic reference data, and two real PDF inputs for procurement verification. The LangGraph package will manage its checkpoint tables when the workflow is integrated in Phase 4.

The local Compose service publishes PostgreSQL on loopback and retains data in a named volume. The seed command takes a transaction-scoped advisory lock, checks applied migration checksums, applies new migrations in filename order, and inserts the reference records. A failure rolls back the transaction. Use `--migrate-only` for an empty schema without synthetic records.

## Data contracts and schemas

| Table | Key and contract |
| --- | --- |
| `vendors` | Stable vendor ID, unique display name, payment terms in days, and contract reference. |
| `price_catalog` | Exact vendor and SKU pair, description, currency, unit, and positive decimal unit price. |
| `procurement_runs` | UUID `run_id` and unique UUID `thread_id`, procurement scope, status, optional verified PO fields, and decision evidence. |
| `processed_events` | Unique lowercase SHA-256 hex digest as primary key, one linked run per digest, and source provenance. |
| `procurement_inputs` and `procurement_jobs` | One PDF per run. A distinct process or resume job tracks attempts, availability, and lease ownership and expiry. |
| `audit_logs` | Run-linked events with a stable event key unique per run, actor, details, and timestamp. This lets later workflow code make repeated writes idempotent. |
| `sop_policy_versions` | One row per policy ID and version. Status, effective date, category, and region live here so rules and chunks use the same scope. |
| `sop_policy_rules` | Versioned numeric rules with explicit codes for payment-term days, Finance Director approval cap, and price variance fraction. Only the monetary cap carries a currency code. |
| `sop_policy_chunks` | Version-linked citation location, passage text, embedding-space identifier, and nonzero `vector(1536)` embedding. |

Apex Industrial Supply has Net-30 terms and the SS-316 Valve at USD 120 per unit. Harbor Flow Components carries the same SKU at a different price, which gives later tool tests a vendor-scope control. Summit Fastener Works provides an additional independently keyed catalog.

| Policy at 2026-10-19 | Status and scope | Rule or test condition |
| --- | --- | --- |
| US Procurement 2026 | Active, US, procurement | Net-30, Finance Director approval above USD 10,000, and unit-price variance above 5%. |
| US Procurement 2024 | Archived, US, procurement | Prior USD 5,000 approval cap. |
| EU Freight 2026 | Active, EU, freight | EUR 7,500 approval cap outside US procurement scope. |
| US Procurement 2027 | Active, US, procurement, effective 2027-01-01 | Future-effective USD 12,500 cap. |

Seed vectors use the deterministic `procurement-fixture-v1` space. They support offline tests and deliberately place ineligible controls close to an approval-cap query. A production embedding provider must use its own space and query only vectors from that space. Phase 2 owns the SQL eligibility and ranking queries.

## Invariants and edge cases

- Bootstrap serializes concurrent invocations and refuses to continue if an applied migration's checksum changes. Rerunning seed fixtures keeps existing rows, including later reviewed edits. Add a migration for schema changes and publish approved policy changes under a new version.
- Monetary catalog values and policy thresholds use PostgreSQL `NUMERIC`. The generator computes line totals with Python `Decimal` and checks the sum against the fixture's declared total before writing the PDF.
- A duplicate digest or missing run reference fails at the database boundary. The Phase 3 endpoint will use those constraints to handle concurrent intake.
- The PDF generator uses a shared catalog source, stable dates, and deterministic PDF metadata. Its overcharge fixture overrides only the quoted valve price. Each PDF contains selectable text for the Phase 3 parser.

| PDF | Expected evidence |
| --- | --- |
| [PO-2026-419_Apex_Overcharge.pdf](../../seed/sample_pdfs/PO-2026-419_Apex_Overcharge.pdf) | Four pages, Net-15, and USD 18,450. Page 3 shows 50 SS-316 Valves at USD 145 each. The USD 25 unit difference produces USD 1,250 overcharge and 20.8% displayed variance. |
| [PO-2026-420_Clean.pdf](../../seed/sample_pdfs/PO-2026-420_Clean.pdf) | One page, Net-30, and USD 4,200. All three lines match the Apex catalog. |

## Verification commands

From the repository root with Docker Compose and Python 3.11 or newer:

```bash
docker compose up -d --wait postgres
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r seed/requirements-test.txt
python -m seed.seed
python -m seed.seed
python -m seed.generate_pdfs --output-dir /tmp/procurement-pdfs
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q seed/tests
```

The database tests use a role with permission to create disposable databases. The local Compose credentials satisfy that requirement. Set `DATABASE_URL` and `TEST_DATABASE_URL` to match any customized password or port. Expect six tests to pass. The suite checks repeatable seed data, transaction rollback, digest and foreign-key constraints, and PDF contents. Without `TEST_DATABASE_URL`, pytest skips the three database tests.

Inspect the extension version, fixture counts, and policy scope directly:

```bash
docker compose exec -T postgres psql -U procurement -d procurement -v ON_ERROR_STOP=1 <<'SQL'
SELECT extversion FROM pg_extension WHERE extname = 'vector';
SELECT (SELECT count(*) FROM vendors) AS vendors,
       (SELECT count(*) FROM price_catalog) AS catalog_rows,
       (SELECT count(*) FROM sop_policy_chunks) AS policy_chunks;
SELECT policy_id, version, status, category, region, effective_date
FROM sop_policy_versions ORDER BY policy_id, version;
SQL
```

Expect three vendors, six catalog rows, six policy chunks, and the four policy versions listed above.
