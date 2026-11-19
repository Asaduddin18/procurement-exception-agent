"""Post an approved, reserved invoice and attach its original PDF."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

import simplejson

from backend.ap_store import APStore
from backend.qbo_client import QBOClient, QBOError, qbo_literal


@dataclass(frozen=True, slots=True)
class PostedBill:
    bill_id: str
    attachment_id: str


class QBOPosting:
    def __init__(self, store: APStore, client: QBOClient, freight_account_id: str | None = None):
        self.store, self.client, self.freight_account_id = store, client, freight_account_id

    async def _approved_invoice(self, invoice_id: UUID):
        async with await self.store.connect() as connection:
            cursor = await connection.execute("""
                SELECT invoice.*, po.qbo_vendor_id, po.po_number AS resolved_po_number, po.is_open,
                       audit.audit_id, audit.details, input.pdf_bytes
                FROM vendor_invoices invoice
                JOIN procurement_runs run USING (run_id)
                JOIN procurement_inputs input USING (run_id)
                JOIN qbo_purchase_orders po USING (realm_id, qbo_po_id)
                JOIN LATERAL (
                    SELECT audit_id, details FROM audit_logs WHERE run_id = run.run_id
                        AND event_type = 'AP_INVOICE_APPROVED'
                    ORDER BY audit_id DESC LIMIT 1
                ) audit ON true
                WHERE invoice.invoice_id = %s AND invoice.realm_id = %s AND run.status = 'APPROVED'
            """, (invoice_id, self.client.realm_id))
            invoice = await cursor.fetchone()
            if invoice is None or invoice["status"] not in ("RESERVED", "POST_UNKNOWN", "POSTED"):
                raise QBOError("An approved invoice with a durable quantity reservation is required.")
            cursor = await connection.execute("""
                SELECT line.*, po.qbo_item_id, po.is_active FROM invoice_line_items line
                LEFT JOIN qbo_purchase_order_lines po USING (realm_id, qbo_po_id, qbo_line_id)
                WHERE line.invoice_id = %s ORDER BY line.line_number
            """, (invoice_id,))
            return invoice, await cursor.fetchall()

    def _bill_payload(self, invoice_id: UUID, invoice: dict, lines: list[dict]) -> dict:
        if not invoice["is_open"] or any(not line["is_active"] for line in lines):
            raise QBOError("The reserved QuickBooks PO or line was closed or removed.")
        bill_lines = []
        for line in lines:
            expected = (line["quantity"] * line["unit_price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if not line["qbo_item_id"] or expected != line["amount"]:
                raise QBOError("Invoice lines must have QuickBooks item mappings and reconciled amounts.")
            bill_lines.append({
                "Amount": line["amount"], "DetailType": "ItemBasedExpenseLineDetail",
                "LinkedTxn": [{
                    "TxnId": invoice["qbo_po_id"], "TxnType": "PurchaseOrder",
                    "TxnLineId": line["qbo_line_id"],
                }],
                "ItemBasedExpenseLineDetail": {
                    "ItemRef": {"value": line["qbo_item_id"]},
                    "Qty": line["quantity"], "UnitPrice": line["unit_price"],
                },
            })
        if invoice["freight_amount"]:
            if not self.freight_account_id:
                raise QBOError("Approved freight needs a configured QuickBooks expense account.")
            bill_lines.append({
                "Amount": invoice["freight_amount"], "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": {"AccountRef": {"value": self.freight_account_id}},
            })
        if not lines or sum(line["Amount"] for line in bill_lines) != invoice["total_amount"]:
            raise QBOError("QuickBooks bill lines must reconcile to the approved invoice total.")
        doc_number = invoice["invoice_number"].strip()
        if "\n" in doc_number or "\r" in doc_number or len(doc_number) > 21:
            raise QBOError("The approved invoice number does not fit the QuickBooks document number.")
        notes = invoice["details"].get("notes", "")
        note = f"Procurement AP {invoice_id} | Audit {invoice['audit_id']} | PO {invoice['resolved_po_number']}"
        if isinstance(notes, str) and notes.strip():
            note += " | " + notes.strip()
        return {
            "VendorRef": {"value": invoice["qbo_vendor_id"]}, "DocNumber": doc_number,
            "CurrencyRef": {"value": invoice["currency"]}, "PrivateNote": note[:4000],
            "LinkedTxn": [{"TxnId": invoice["qbo_po_id"], "TxnType": "PurchaseOrder"}],
            "Line": bill_lines,
        }

    @staticmethod
    def _matches_bill(bill: dict, payload: dict, invoice_id: UUID, total: Decimal) -> bool:
        return (
            bill.get("VendorRef", {}).get("value") == payload["VendorRef"]["value"]
            and bill.get("DocNumber") == payload["DocNumber"]
            and str(bill.get("PrivateNote", "")).startswith(f"Procurement AP {invoice_id} | ")
            and bill.get("TotalAmt") == total
        )

    async def _find_bill(self, payload: dict, invoice_id: UUID, total: Decimal) -> dict | None:
        candidates = [row async for row in self.client.query(
            "Bill", f" WHERE DocNumber = {qbo_literal(payload['DocNumber'])}",
        ) if row.get("VendorRef", {}).get("value") == payload["VendorRef"]["value"]]
        if not candidates:
            return None
        if len(candidates) != 1 or not self._matches_bill(candidates[0], payload, invoice_id, total):
            raise QBOError("A QuickBooks bill with this vendor and number needs manual reconciliation.")
        return candidates[0]

    async def _bill(self, invoice_id: UUID, invoice: dict, lines: list[dict]) -> str:
        if invoice["qbo_bill_id"]:
            return invoice["qbo_bill_id"]
        total = invoice["total_amount"]
        if invoice["status"] == "RESERVED":
            payload = self._bill_payload(invoice_id, invoice, lines)
        else:
            payload = {
                "VendorRef": {"value": invoice["qbo_vendor_id"]},
                "DocNumber": invoice["invoice_number"].strip(),
            }
        bill = await self._find_bill(payload, invoice_id, total)
        if invoice["status"] == "RESERVED":
            if bill is not None:
                raise QBOError("A bill exists for this reserved invoice. Review its reconciliation state.")
            if await self.store.begin_post(invoice_id):
                try:
                    response = await self.client.request(
                        "POST", "/bill", params={"requestid": f"ap-bill-{invoice_id}"},
                        headers={"Content-Type": "application/json"}, content=simplejson.dumps(payload),
                    )
                except QBOError as error:
                    if error.status == 429:
                        await self.store.record_post_throttled(invoice_id)
                    raise
                bill = response.get("Bill")
            else:
                bill = await self._find_bill(payload, invoice_id, total)
        if not bill or not bill.get("Id") or not self._matches_bill(bill, payload, invoice_id, total):
            raise QBOError("QuickBooks bill outcome is unresolved. The reservation is retained.")
        if not await self.store.record_posted(invoice_id, bill["Id"]):
            raise QBOError("The confirmed QuickBooks bill conflicts with this ledger entry.")
        return bill["Id"]

    @staticmethod
    def _matches_attachment(row: dict, file_name: str, bill_id: str) -> bool:
        return row.get("FileName") == file_name and any(
            ref.get("EntityRef", {}).get("type") == "Bill" and ref["EntityRef"].get("value") == bill_id
            for ref in row.get("AttachableRef", [])
        )

    async def _attachment(self, invoice_id: UUID, bill_id: str, pdf: bytes) -> str:
        async with await self.store.connect() as connection:
            cursor = await connection.execute("""
                INSERT INTO qbo_bill_attachments (invoice_id) VALUES (%s)
                ON CONFLICT DO NOTHING RETURNING invoice_id
            """, (invoice_id,))
            claimed = await cursor.fetchone() is not None
            cursor = await connection.execute("""
                SELECT qbo_attachable_id FROM qbo_bill_attachments WHERE invoice_id = %s
            """, (invoice_id,))
            existing = (await cursor.fetchone())["qbo_attachable_id"]
        if existing:
            return existing

        file_name = f"procurement-ap-{invoice_id}.pdf"
        if claimed:
            metadata = {
                "FileName": file_name, "ContentType": "application/pdf",
                "AttachableRef": [{"EntityRef": {"type": "Bill", "value": bill_id}}],
            }
            try:
                response = await self.client.request(
                    "POST", "/upload", params={"requestid": f"ap-pdf-{invoice_id}"},
                    files={
                        "file_metadata_01": ("attachment.json", simplejson.dumps(metadata), "application/json"),
                        "file_content_01": (file_name, pdf, "application/pdf"),
                    },
                )
            except QBOError as error:
                if error.status in (400, 401, 403, 404, 413, 429):
                    async with await self.store.connect() as connection:
                        await connection.execute("""
                            DELETE FROM qbo_bill_attachments
                            WHERE invoice_id = %s AND qbo_attachable_id IS NULL
                        """, (invoice_id,))
                raise
            candidates = [row.get("Attachable", {}) for row in response.get("AttachableResponse", [])]
        else:
            candidates = [row async for row in self.client.query(
                "Attachable", f" WHERE FileName = {qbo_literal(file_name)}",
            )]
        matches = [row["Id"] for row in candidates if row.get("Id") and self._matches_attachment(row, file_name, bill_id)]
        if len(matches) != 1:
            raise QBOError("The bill is posted but its PDF attachment is unresolved.")
        async with await self.store.connect() as connection:
            cursor = await connection.execute("""
                UPDATE qbo_bill_attachments SET qbo_attachable_id = %s
                WHERE invoice_id = %s AND (qbo_attachable_id IS NULL OR qbo_attachable_id = %s)
            """, (matches[0], invoice_id, matches[0]))
            if cursor.rowcount != 1:
                raise QBOError("The confirmed QuickBooks PDF conflicts with this ledger entry.")
        return matches[0]

    async def post_approved_invoice(self, invoice_id: UUID) -> PostedBill:
        invoice, lines = await self._approved_invoice(invoice_id)
        pdf = invoice["pdf_bytes"]
        if not pdf.startswith(b"%PDF-"):
            raise QBOError("The original PDF is required before creating a QuickBooks bill.")
        bill_id = await self._bill(invoice_id, invoice, lines)
        attachment_id = await self._attachment(invoice_id, bill_id, pdf)
        return PostedBill(bill_id, attachment_id)
