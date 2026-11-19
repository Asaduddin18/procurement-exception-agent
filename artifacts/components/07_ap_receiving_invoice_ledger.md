# AP Receiving and Invoice Ledger

## Purpose & Scope

The ledger stores the three inputs to AP matching and reserves received quantities before a posting worker can submit a QuickBooks bill. The Phase 2 store accepts structured invoice lines and receipt events. The [QuickBooks adapter](08_qbo_adapter.md) consumes the reservation when posting an approved invoice. Invoice extraction, price and freight policy, and the AP Control Tower follow in later phases.

```mermaid
flowchart TD
    PO["QuickBooks PO and line snapshots"] --> M["Receipt-backed reservation"]
    GR["Warehouse receipt events"] --> M
    PDF["PDF intake and SHA-256"] --> ID["Vendor invoice and item lines"]
    ID --> M
    M -->|Available| R["Reserved invoice"]
    M -->|Missing or insufficient evidence| H["Invoice held for review"]
    R --> U["Posting attempt recorded"]
    U -->|Confirmed bill ID| B["Posted invoice"]
```

## Data Contracts / Schemas

| Record | Key and constraints |
| --- | --- |
| `qbo_purchase_orders` | QuickBooks realm and PO IDs. Stores the display PO number, internal and QuickBooks vendor IDs, currency, open status, and sync timestamp. |
| `qbo_purchase_order_lines` | Realm, PO ID, and QuickBooks line ID. Stores item ID, SKU, unit, ordered quantity, and unit price. Repeated SKUs retain separate balances when the source line IDs differ. |
| `goods_receipts` | Realm, warehouse source, and stable event ID. Each event records a positive quantity in the PO line's unit and its receipt timestamp. Exact replay returns the existing result. A reused ID with a different line, quantity, or timestamp returns a conflict. |
| `vendor_invoices` | One invoice per realm, vendor, and normalized invoice number. References the existing SHA-256 event ledger by run ID. Stores the printed PO number, resolved PO ID when available, currency, freight, total, posting state, and confirmed bill ID. |
| `invoice_line_items` | Invoice ID and line number. Optional PO links must refer to the invoice's own resolved PO. The store checks SKU and unit against the linked PO line before reserving. |
| `qbo_tokens` | One row per realm with separate access and refresh ciphertext fields and expiration timestamps. The adapter encrypts values before writing and keeps the encryption key outside PostgreSQL. |

Invoice numbers are case-normalized and trimmed at their ends. The same normalized identity, currency, and total return the existing invoice ID as a duplicate, even when a re-export changes the PDF hash. The same identity with a different total or currency returns an identity conflict without changing the canonical invoice. Header and lines commit together. Dollars and quantities use exact PostgreSQL numeric values, with scale checks to catch precision loss at insertion.

## Reservation and Failure Handling

1. Lock the invoice row and check that it has a resolved, open PO. Lock the referenced PO lines in stable ID order. Reject missing links, SKU mismatches, and unit mismatches.
2. For each PO line, calculate available units as the lesser of ordered quantity and cumulative received quantity, minus quantities on invoices in `RESERVED`, `POST_UNKNOWN`, and `POSTED`. Sum every line on the candidate invoice that references the same PO line.
3. At PostgreSQL's read committed isolation level, calculate capacity in a separate statement after locking. This lets a waiting transaction see the competing transaction's committed reservation. Reserve the entire invoice only if every line fits.
4. A reservation can be released before a posting attempt begins. The posting adapter atomically transitions `RESERVED` to `POST_UNKNOWN` before calling QuickBooks. A second worker loses that transition. Ambiguous outcomes retain capacity until reconciliation. A confirmed bill ID advances the invoice to `POSTED`. Replaying the same confirmation is safe.

The receipt contract is additive. Source-side corrections require a separate adjustment contract before a negative quantity can affect available stock. Receipt totals above the order quantity never increase billable capacity beyond the ordered quantity.

## Verification Commands

Run against PostgreSQL with pgvector and an account that can create disposable databases:

```bash
docker compose up -d --wait postgres
python -m pip install -r seed/requirements-test.txt -r backend/requirements.txt
TEST_DATABASE_URL=postgresql://procurement:procurement@127.0.0.1:5432/procurement \
  python -m pytest -q backend/tests/test_ap_store.py seed/tests/test_database.py
```

The integration suite covers concurrent receipt replay, changed receipt payloads, same-byte and re-exported invoice deduplication, changed-total conflicts, atomic registration, repeated SKUs on independent PO lines, and races for the last received units. No QuickBooks credentials are required for these checks.
