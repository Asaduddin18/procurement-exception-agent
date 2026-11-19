"""Extract a persisted PDF, register its business identity, and record AP match evidence."""

from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from backend.ap_store import APStore, InvoiceLine
from backend.extraction import ExtractionError, parse_pdf
from backend.invoice_matching import load_match_evidence, match_invoice
from backend.invoice_models import InvoiceEvidence, MatchFinding, MatchPolicy, MatchResult


async def evaluate_invoice_run(store: APStore, run_id: UUID, realm_id: str, extract, policy: MatchPolicy):
    async with await store.connect() as connection:
        cursor = await connection.execute("SELECT pdf_bytes FROM procurement_inputs WHERE run_id = %s", (run_id,))
        row = await cursor.fetchone()
    if row is None:
        raise ExtractionError("missing_pdf_input")
    pages = await parse_pdf(row["pdf_bytes"])
    document = (await extract(pages)).document
    evidence = {}
    if not isinstance(document, InvoiceEvidence):
        result = MatchResult(findings=[MatchFinding(code=f"{document.document_type}_BLOCKED")])
    else:
        if document.total_source_page > len(pages) or any(line.source_page > len(pages) for line in document.lines):
            raise ExtractionError("invalid_page_reference")
        evidence = await load_match_evidence(store.database_url, realm_id, document, run_id)
        result = match_invoice(document, evidence, policy)
        async with await store.connect() as connection:
            vendor = await connection.execute("SELECT vendor_id FROM vendors WHERE vendor_name = %s", (document.vendor_name,))
            vendor_row = await vendor.fetchone()
        if vendor_row is None:
            result.findings.append(MatchFinding(code="VENDOR_NOT_FOUND"))
        else:
            order = evidence["orders"][0] if len(evidence["orders"]) == 1 else None
            mapped = {}
            for line in evidence["lines"]:
                if line["is_active"]:
                    mapped.setdefault((line["sku"], line["unit"]), []).append(line["qbo_line_id"])
            lines = []
            for line in document.lines:
                candidates = mapped.get((line.sku, line.unit), [])
                lines.append(InvoiceLine(
                    line.sku, line.unit, line.quantity, line.unit_price, line.line_total,
                    candidates[0] if len(candidates) == 1 else None,
                ))
            registration = await store.register_invoice(
                run_id=run_id, realm_id=realm_id, vendor_id=vendor_row["vendor_id"],
                invoice_number=document.invoice_number, currency=document.currency,
                freight_amount=document.freight_amount, total_amount=document.total_amount,
                po_number=document.po_number,
                qbo_po_id=order["qbo_po_id"] if order and order["currency"] == document.currency else None,
                lines=lines if order and order["currency"] == document.currency else [
                    InvoiceLine(line.sku, line.unit, line.quantity, line.unit_price, line.amount)
                    for line in lines
                ],
            )
            if registration.status != "registered":
                async with await store.connect() as connection:
                    canonical = await connection.execute(
                        "SELECT run_id FROM vendor_invoices WHERE invoice_id = %s",
                        (registration.invoice_id,),
                    )
                    same_run = (await canonical.fetchone())["run_id"] == run_id
                if registration.status == "identity_conflict" or not same_run:
                    result.findings.append(MatchFinding(code=registration.status.upper()))
    async with await store.connect() as connection:
        await connection.execute("""
            INSERT INTO ap_document_evaluations (run_id, realm_id, document_type, evidence, findings, policy)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                realm_id = excluded.realm_id, document_type = excluded.document_type,
                evidence = excluded.evidence, findings = excluded.findings, policy = excluded.policy,
                evaluated_at = now()
        """, (run_id, realm_id, document.document_type, Jsonb({"document": document.model_dump(mode="json"),
                    "match_snapshot": TypeAdapter(dict).dump_python(evidence, mode="json")}),
              Jsonb(result.model_dump(mode="json")), Jsonb(policy.model_dump(mode="json"))))
    return result
