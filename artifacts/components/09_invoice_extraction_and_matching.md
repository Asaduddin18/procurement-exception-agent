# Invoice Extraction and Three-Way Matching

## Purpose & Scope

Phase 4 adds invoice and statement classification, validated invoice extraction, exact SQL evidence reads, and deterministic matching. The service accepts an existing intake run ID and a QuickBooks realm, loads the original PDF, extracts each page, registers the invoice identity, and persists the evaluation. The next phase connects this service to the invoice route in LangGraph, reviewer actions, and the Control Tower.

```mermaid
flowchart TD
    PDF["Persisted PDF and run"] --> X["Page parser and DeepSeek extraction"]
    X --> C{"Document type"}
    C -->|Statement or other| B["Persist blocked result"]
    C -->|Invoice| V["Pydantic totals and page validation"]
    V --> SQL["Exact vendor, PO, receipt and billing reads"]
    SQL --> M["Decimal matching rules"]
    M --> ID["Register invoice identity"]
    ID --> R["Persist findings, source snapshot and policy"]
```

## Data Contracts / Schemas

The structured extraction envelope has a discriminated `document` field. Invoices contain vendor name, invoice and PO numbers, currency, item lines with unit, quantity, unit price, printed line total and source page, plus freight, tax, discount, printed total and its source page. A statement or other document returns only its classification.

Pydantic v2 validates positive quantities, exact decimal inputs, unique line numbers, rounded line multiplication, and the sum of lines plus header adjustments. The service rejects evidence pages outside the parsed PDF. The existing purchase order schema and extraction entry point share the same structured-output client and retry behavior.

The `MatchPolicy` contract defaults to a 1% price fraction, $2 per-unit absolute tolerance, and zero authorized freight. Price escalation requires the positive difference to exceed both limits. Equality passes. At a $120 reference price, $122 passes and $122.01 flags. This tolerance is independently checked against both the PO price and the exact vendor catalog price.

## Invariants & Edge Cases

- Match the vendor and PO number within the configured QuickBooks realm by exact SQL. Missing or multiple PO records block matching. Check open status and currency.
- Keep repeated source PO lines independent. If one SKU appears on several active PO lines, flag ambiguity for review. A single invoice can repeat a SKU. Sum its requested quantity before comparing against remaining received units.
- Available quantity is the lesser of ordered and received quantity minus `RESERVED`, `POST_UNKNOWN`, and `POSTED` allocations. A reservation in the later approval flow must recheck capacity under the existing PO-line locks because evidence can change after evaluation.
- Require compatible units and catalog currency. Header freight is compared to an explicit authorized amount. Tax and discount require review because their allocation is outside the current posting payload.
- Register the invoice identity through the existing AP ledger. A re-exported PDF with the same vendor, invoice number, currency and total reports `DUPLICATE`. A changed total or currency reports `IDENTITY_CONFLICT`. Neither replaces the canonical invoice.
- Block account statements before invoice registration. Persist the extracted document, the SQL match snapshot, the policy values, and findings in `ap_document_evaluations`.
- The evaluation service requires a trusted internal caller to supply the realm and policy. The upcoming API and graph integration will derive those values from deployment configuration and reviewer authorization.

## Verification Commands

Use PostgreSQL with pgvector and a disposable-database-capable test account.

```bash
docker compose up -d --wait postgres
python -m seed.seed --migrate-only
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q backend/tests/test_invoice_matching.py
```

The tests exercise exact totals, float and NaN rejection, both sides of the price boundary, partial billing, accumulated quantities, repeated SKUs, ambiguous PO lines, unit and currency mismatches, freight, structured extraction through a simulated DeepSeek response, statement blocking, duplicate registration, and persistence against real PostgreSQL.

```sql
SELECT run_id, document_type, findings, policy
FROM ap_document_evaluations
ORDER BY evaluated_at DESC;
```
