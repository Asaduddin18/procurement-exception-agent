"""Exact SQL evidence reads and deterministic AP three-way comparison."""

from decimal import Decimal, localcontext
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from backend.invoice_models import InvoiceEvidence, MatchFinding, MatchPolicy, MatchResult


def match_invoice(invoice: InvoiceEvidence, evidence: dict, policy: MatchPolicy) -> MatchResult:
    result = MatchResult()

    def flag(code, line_number=None, observed=None, expected=None):
        result.findings.append(MatchFinding(
            code=code, line_number=line_number,
            observed=str(observed) if observed is not None else None,
            expected=str(expected) if expected is not None else None,
        ))

    if len(evidence["orders"]) != 1:
        flag("PO_MISSING_OR_AMBIGUOUS")
        return result
    order = evidence["orders"][0]
    if not order["is_open"]:
        flag("PO_CLOSED")
    if order["currency"] != invoice.currency:
        flag("CURRENCY_MISMATCH", observed=invoice.currency, expected=order["currency"])
    if invoice.tax_amount or invoice.discount_amount:
        flag("TAX_OR_DISCOUNT_REVIEW")
    if invoice.freight_amount > policy.authorized_freight:
        flag("FREIGHT_UNAUTHORIZED", observed=invoice.freight_amount, expected=policy.authorized_freight)

    requested = {}
    for line in invoice.lines:
        matches = [row for row in evidence["lines"] if row["sku"] == line.sku and row["is_active"]]
        if len(matches) != 1:
            flag("SKU_MISSING_OR_AMBIGUOUS", line.line_number)
            continue
        po_line = matches[0]
        if line.unit != po_line["unit"]:
            flag("UOM_MISMATCH", line.line_number, line.unit, po_line["unit"])
            continue
        if po_line["catalog_price"] is None or po_line["catalog_unit"] != line.unit:
            flag("CATALOG_EVIDENCE_MISSING", line.line_number)
            continue
        with localcontext(prec=50):
            for code, expected in (("PO_PRICE_VARIANCE", po_line["unit_price"]),
                                   ("CONTRACT_PRICE_VARIANCE", po_line["catalog_price"])):
                tolerance = max(expected * policy.price_fraction, policy.unit_price_absolute)
                if line.unit_price - expected > tolerance:
                    flag(code, line.line_number, line.unit_price, expected)
            key = po_line["qbo_line_id"]
            requested[key] = requested.get(key, Decimal(0)) + line.quantity
            available = min(po_line["ordered_quantity"], po_line["received_quantity"]) - po_line["allocated_quantity"]
            if requested[key] > available:
                flag("QUANTITY_EXCEEDS_AVAILABLE", line.line_number, requested[key], max(Decimal(0), available))
    return result


async def load_match_evidence(database_url: str, realm_id: str, invoice: InvoiceEvidence, run_id: UUID) -> dict:
    async with await psycopg.AsyncConnection.connect(database_url, row_factory=dict_row) as connection:
        await connection.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
        cursor = await connection.execute("""
            SELECT po.* FROM qbo_purchase_orders po JOIN vendors USING (vendor_id)
            WHERE realm_id = %s AND po_number = %s AND vendor_name = %s
        """, (realm_id, invoice.po_number, invoice.vendor_name))
        orders = await cursor.fetchall()
        if len(orders) != 1:
            return {"orders": orders, "lines": []}
        order = orders[0]
        cursor = await connection.execute("""
            SELECT line.*, catalog.unit_price AS catalog_price, catalog.unit AS catalog_unit,
                COALESCE((
                    SELECT sum(received_quantity) FROM goods_receipts receipt
                    WHERE (receipt.realm_id, receipt.qbo_po_id, receipt.qbo_line_id) =
                          (line.realm_id, line.qbo_po_id, line.qbo_line_id)
                ), 0) AS received_quantity,
                COALESCE((
                    SELECT sum(item.quantity) FROM invoice_line_items item
                    JOIN vendor_invoices invoice USING (invoice_id)
                    WHERE (item.realm_id, item.qbo_po_id, item.qbo_line_id) =
                          (line.realm_id, line.qbo_po_id, line.qbo_line_id)
                      AND invoice.status IN ('RESERVED', 'POST_UNKNOWN', 'POSTED')
                      AND invoice.run_id <> %s
                ), 0) AS allocated_quantity
            FROM qbo_purchase_order_lines line
            LEFT JOIN price_catalog catalog ON catalog.vendor_id = %s AND catalog.sku = line.sku
                                               AND catalog.currency = %s
            WHERE line.realm_id = %s AND line.qbo_po_id = %s
        """, (run_id, order["vendor_id"], order["currency"], realm_id, order["qbo_po_id"]))
        return {"orders": orders, "lines": await cursor.fetchall()}
