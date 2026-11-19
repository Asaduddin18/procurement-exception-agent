"""Checkpointed invoice evaluation, reviewer resolution, and reservation handoff."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.ap_store import APStore
from backend.invoice_evaluation import evaluate_invoice_run
from backend.invoice_matching import load_match_evidence, match_invoice
from backend.invoice_models import InvoiceEvidence, MatchPolicy
from backend.review_graph import ProcurementState, timed_node
from backend.review_models import Decision, Finding, Resolution, ReviewOutcome

REVIEWABLE = {"PO_PRICE_VARIANCE", "CONTRACT_PRICE_VARIANCE", "FREIGHT_UNAUTHORIZED"}


async def invoice_detail(database_url: str, run_id: UUID) -> dict | None:
    async with await APStore(database_url).connect() as connection:
        cursor = await connection.execute("""
            SELECT evidence, findings, policy FROM ap_document_evaluations WHERE run_id = %s
        """, (run_id,))
        evaluation = await cursor.fetchone()
        if evaluation is None:
            return None
        cursor = await connection.execute("""
            SELECT i.invoice_id, i.status, i.qbo_bill_id, a.qbo_attachable_id
            FROM vendor_invoices i LEFT JOIN qbo_bill_attachments a USING (invoice_id)
            WHERE i.run_id = %s
        """, (run_id,))
        return {**evaluation, "posting": await cursor.fetchone()}


def build_invoice_graph(checkpointer, store: APStore, realm_id: str, extract, policy: MatchPolicy):
    async def evaluate_invoice(state):
        run_id = UUID(state["run_id"])
        result = await evaluate_invoice_run(store, run_id, realm_id, extract, policy)
        detail = await invoice_detail(store.database_url, run_id)
        doc = detail["evidence"]["document"]
        findings = [Finding(**item.model_dump()) for item in result.findings]
        decision = Decision(
            evaluated_on=date.today(), findings=findings,
            approval_blocked=any(item.code not in REVIEWABLE for item in result.findings),
            approval_tier="FINANCE_DIRECTOR" if Decimal(doc.get("total_amount", "0")) > 10000
            else "PROCUREMENT_REVIEWER",
        )
        if Decimal(doc.get("total_amount", "0")) > 10000:
            decision.findings.append(Finding(code="FINANCE_APPROVAL_REQUIRED", observed=doc["total_amount"], expected="10000"))
        async with await store.connect() as connection:
            await connection.execute("""
                UPDATE procurement_runs SET po_number = %s, currency = %s, grand_total = %s,
                    vendor_id = (SELECT vendor_id FROM vendors WHERE vendor_name = %s)
                WHERE run_id = %s
            """, (doc.get("po_number"), doc.get("currency"), doc.get("total_amount"), doc.get("vendor_name"), run_id))
        return {"decision": decision.model_dump(mode="json")}

    def review_invoice(state):
        return {"resolution": Resolution.model_validate(interrupt(state["decision"])).model_dump(mode="json")}

    async def finalize_invoice(state):
        run_id = UUID(state["run_id"])
        decision = Decision.model_validate(state["decision"])
        resolution = Resolution.model_validate(state["resolution"]) if state.get("resolution") else None
        action = resolution.action if resolution else "APPROVE"
        status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "DISPUTE": "DISPUTED"}[action]
        detail = await invoice_detail(store.database_url, run_id)
        draft = None
        if action == "APPROVE":
            if decision.approval_blocked or (decision.approval_tier == "FINANCE_DIRECTOR"
                    and (resolution is None or resolution.role != "FINANCE_DIRECTOR")):
                raise ValueError("Invoice approval authority or evidence is insufficient.")
            invoice = InvoiceEvidence.model_validate(detail["evidence"]["document"])
            fresh = await load_match_evidence(store.database_url, realm_id, invoice, run_id)
            findings = match_invoice(invoice, fresh, policy).findings
            approved = {
                (finding.code, finding.line_number, finding.observed, finding.expected)
                for finding in decision.findings if resolution and finding.code in REVIEWABLE
            }
            remaining = [
                finding for finding in findings
                if (finding.code, finding.line_number, finding.observed, finding.expected) not in approved
            ]
            if remaining:
                return {"outcome": ReviewOutcome(status="FAILED", decision=decision, resolution=resolution,
                                                error_code="invoice_evidence_changed").model_dump(mode="json")}
            posting = detail["posting"]
            if posting is None or await store.reserve_invoice(posting["invoice_id"]) not in ("reserved", "already_reserved"):
                return {"outcome": ReviewOutcome(status="FAILED", decision=decision, resolution=resolution,
                                                error_code="invoice_capacity_changed").model_dump(mode="json")}
        elif action == "DISPUTE":
            document = detail["evidence"]["document"]
            lines = {row["line_number"]: row for row in document.get("lines", [])}
            references = []
            for finding in decision.findings:
                line = lines.get(finding.line_number, {})
                references.append(
                    f"- {finding.code}: SKU {line.get('sku', 'header')}, "
                    f"observed {finding.observed or 'unavailable'}, expected {finding.expected or 'review required'} "
                    f"[run {run_id}, invoice line {finding.line_number or 'header'}]"
                )
            draft = (
                f"Subject: Invoice {document.get('invoice_number', 'document')} — PO {document.get('po_number', 'unavailable')}\n\n"
                "Please review the following discrepancies against our purchase order and receiving records.\n"
                + "\n".join(references) + "\n\nPlease provide a corrected invoice or supporting evidence."
            )
        return {"outcome": ReviewOutcome(status=status, decision=decision, resolution=resolution,
                                         dispute_draft=draft).model_dump(mode="json")}

    graph = StateGraph(ProcurementState)
    for node in (evaluate_invoice, review_invoice, finalize_invoice):
        graph.add_node(node.__name__, timed_node(node))
    graph.add_edge(START, "evaluate_invoice")
    graph.add_conditional_edges("evaluate_invoice",
        lambda state: "review_invoice" if state["decision"]["findings"] else "finalize_invoice",
        ["review_invoice", "finalize_invoice"])
    graph.add_edge("review_invoice", "finalize_invoice")
    graph.add_edge("finalize_invoice", END)
    return graph.compile(checkpointer=checkpointer)
