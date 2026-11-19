"""Durable AP identities and receipt-backed reservations for the posting worker."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    sku: str
    unit: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    qbo_line_id: str | None = None


@dataclass(frozen=True, slots=True)
class InvoiceRegistration:
    invoice_id: UUID
    status: Literal["registered", "duplicate", "identity_conflict"]


class APStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def connect(self):
        connection = await psycopg.AsyncConnection.connect(
            self.database_url, row_factory=dict_row, connect_timeout=3,
            options="-c statement_timeout=5000",
        )
        await connection.set_isolation_level(psycopg.IsolationLevel.READ_COMMITTED)
        return connection

    async def record_receipt(
        self, *, realm_id: str, source_system: str, receipt_event_id: str,
        qbo_po_id: str, qbo_line_id: str, quantity: Decimal, received_at: datetime,
    ) -> Literal["recorded", "duplicate", "identity_conflict"]:
        key = (realm_id, source_system, receipt_event_id)
        receipt = (qbo_po_id, qbo_line_id, quantity, received_at)
        async with await self.connect() as connection:
            inserted = await connection.execute("""
                INSERT INTO goods_receipts (
                    realm_id, source_system, receipt_event_id, qbo_po_id, qbo_line_id,
                    received_quantity, received_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING receipt_event_id
            """, (*key, *receipt))
            if await inserted.fetchone() is not None:
                return "recorded"
            existing = await connection.execute("""
                SELECT qbo_po_id, qbo_line_id, received_quantity, received_at FROM goods_receipts
                WHERE (realm_id, source_system, receipt_event_id) = (%s, %s, %s)
            """, key)
            row = await existing.fetchone()
            return "duplicate" if tuple(row.values()) == receipt else "identity_conflict"

    async def register_invoice(
        self, *, run_id: UUID, realm_id: str, vendor_id: str, invoice_number: str,
        currency: str, freight_amount: Decimal, total_amount: Decimal, lines: Sequence[InvoiceLine],
        po_number: str | None = None, qbo_po_id: str | None = None,
    ) -> InvoiceRegistration:
        if not lines:
            raise ValueError("Invoice registration requires at least one item line.")

        invoice_id = uuid4()
        async with await self.connect() as connection:
            inserted = await connection.execute("""
                INSERT INTO vendor_invoices (
                    invoice_id, run_id, realm_id, vendor_id, invoice_number, po_number, qbo_po_id,
                    currency, freight_amount, total_amount
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING RETURNING invoice_id
            """, (invoice_id, run_id, realm_id, vendor_id, invoice_number, po_number, qbo_po_id,
                  currency, freight_amount, total_amount))
            if await inserted.fetchone() is None:
                existing = await connection.execute("""
                    SELECT invoice_id, total_amount, currency,
                           (realm_id, vendor_id, invoice_number_key) =
                               (%s, %s, upper(btrim(%s))) AS same_identity
                    FROM vendor_invoices
                    WHERE run_id = %s OR (realm_id, vendor_id, invoice_number_key) =
                                         (%s, %s, upper(btrim(%s)))
                    ORDER BY (run_id = %s) DESC
                    LIMIT 1
                """, (realm_id, vendor_id, invoice_number, run_id, realm_id, vendor_id,
                      invoice_number, run_id))
                row = await existing.fetchone()
                same_invoice = (row["same_identity"] and row["currency"] == currency
                                and row["total_amount"] == total_amount)
                return InvoiceRegistration(
                    row["invoice_id"], "duplicate" if same_invoice else "identity_conflict",
                )

            async with connection.cursor() as cursor:
                await cursor.executemany("""
                    INSERT INTO invoice_line_items (
                        invoice_id, line_number, sku, unit, quantity, unit_price, amount,
                        realm_id, qbo_po_id, qbo_line_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, [
                    (invoice_id, number, line.sku, line.unit, line.quantity, line.unit_price, line.amount,
                     realm_id if line.qbo_line_id else None, qbo_po_id if line.qbo_line_id else None,
                     line.qbo_line_id)
                    for number, line in enumerate(lines, start=1)
                ])
        return InvoiceRegistration(invoice_id, "registered")

    async def reserve_invoice(
        self, invoice_id: UUID,
    ) -> Literal["reserved", "already_reserved", "not_ready", "insufficient_quantity"]:
        async with await self.connect() as connection:
            cursor = await connection.execute("""
                SELECT realm_id, qbo_po_id, status FROM vendor_invoices
                WHERE invoice_id = %s FOR UPDATE
            """, (invoice_id,))
            invoice = await cursor.fetchone()
            if invoice is None:
                return "not_ready"
            if invoice["status"] != "RECEIVED":
                return "already_reserved"
            if invoice["qbo_po_id"] is None:
                return "not_ready"

            cursor = await connection.execute("""
                SELECT is_open FROM qbo_purchase_orders
                WHERE (realm_id, qbo_po_id) = (%s, %s) FOR SHARE
            """, (invoice["realm_id"], invoice["qbo_po_id"]))
            if not (await cursor.fetchone())["is_open"]:
                return "not_ready"

            # Acquire locks in stable PO-line order. The capacity query needs a fresh snapshot
            # after waiting, so it runs as a separate READ COMMITTED statement.
            await connection.execute("""
                SELECT po.qbo_line_id FROM qbo_purchase_order_lines po
                WHERE (po.realm_id, po.qbo_po_id) = (%s, %s)
                  AND EXISTS (
                      SELECT 1 FROM invoice_line_items line
                      WHERE line.invoice_id = %s AND line.qbo_line_id = po.qbo_line_id
                  )
                ORDER BY po.qbo_line_id FOR NO KEY UPDATE
            """, (invoice["realm_id"], invoice["qbo_po_id"], invoice_id))

            cursor = await connection.execute("""
                WITH requested AS (
                    SELECT qbo_line_id, sum(quantity) AS quantity
                    FROM invoice_line_items WHERE invoice_id = %(invoice_id)s GROUP BY qbo_line_id
                )
                SELECT
                    EXISTS (SELECT 1 FROM requested) AND NOT EXISTS (
                        SELECT 1 FROM invoice_line_items line
                        LEFT JOIN qbo_purchase_order_lines po USING (realm_id, qbo_po_id, qbo_line_id)
                        WHERE line.invoice_id = %(invoice_id)s AND
                            (po.qbo_line_id IS NULL OR NOT po.is_active OR po.sku <> line.sku OR po.unit <> line.unit)
                    ) AS matched,
                    NOT EXISTS (
                        SELECT 1 FROM requested req
                        JOIN qbo_purchase_order_lines po ON
                            (po.realm_id, po.qbo_po_id, po.qbo_line_id) =
                            (%(realm_id)s, %(qbo_po_id)s, req.qbo_line_id)
                        WHERE req.quantity > LEAST(po.ordered_quantity, COALESCE((
                            SELECT sum(received_quantity) FROM goods_receipts receipt
                            WHERE (receipt.realm_id, receipt.qbo_po_id, receipt.qbo_line_id) =
                                  (po.realm_id, po.qbo_po_id, po.qbo_line_id)
                        ), 0)) - COALESCE((
                            SELECT sum(line.quantity) FROM invoice_line_items line
                            JOIN vendor_invoices invoice USING (invoice_id)
                            WHERE (line.realm_id, line.qbo_po_id, line.qbo_line_id) =
                                  (po.realm_id, po.qbo_po_id, po.qbo_line_id)
                              AND invoice.status IN ('RESERVED', 'POST_UNKNOWN', 'POSTED')
                        ), 0)
                    ) AS within_capacity
            """, {**invoice, "invoice_id": invoice_id})
            capacity = await cursor.fetchone()
            if not capacity["matched"]:
                return "not_ready"
            if not capacity["within_capacity"]:
                return "insufficient_quantity"

            await connection.execute(
                "UPDATE vendor_invoices SET status = 'RESERVED' WHERE invoice_id = %s",
                (invoice_id,),
            )
        return "reserved"

    async def release_unsubmitted(self, invoice_id: UUID) -> bool:
        """Free a reservation only while no posting attempt has started."""
        return await self._transition(invoice_id, "RESERVED", "RECEIVED")

    async def begin_post(self, invoice_id: UUID) -> bool:
        """Persist the unknown outcome before the caller attempts a network write."""
        return await self._transition(invoice_id, "RESERVED", "POST_UNKNOWN")

    async def record_post_throttled(self, invoice_id: UUID) -> bool:
        """Permit a retry after QuickBooks explicitly rejects the write with HTTP 429."""
        return await self._transition(invoice_id, "POST_UNKNOWN", "RESERVED")

    async def _transition(self, invoice_id: UUID, current: str, next_status: str) -> bool:
        async with await self.connect() as connection:
            result = await connection.execute("""
                UPDATE vendor_invoices SET status = %s
                WHERE invoice_id = %s AND status = %s
            """, (next_status, invoice_id, current))
            return result.rowcount == 1

    async def record_posted(self, invoice_id: UUID, qbo_bill_id: str) -> bool:
        """Record a confirmed bill or acknowledge a replay with the same QBO ID."""
        async with await self.connect() as connection:
            result = await connection.execute("""
                UPDATE vendor_invoices SET status = 'POSTED', qbo_bill_id = %s
                WHERE invoice_id = %s AND status = 'POST_UNKNOWN'
            """, (qbo_bill_id, invoice_id))
            if result.rowcount == 1:
                return True
            cursor = await connection.execute("""
                SELECT 1 FROM vendor_invoices
                WHERE invoice_id = %s AND status = 'POSTED' AND qbo_bill_id = %s
            """, (invoice_id, qbo_bill_id))
            return await cursor.fetchone() is not None
