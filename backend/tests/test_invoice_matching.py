import asyncio
import json
from copy import deepcopy
from decimal import Decimal

import httpx
import psycopg
import pytest
from pydantic import ValidationError

from backend.ap_store import APStore
from backend.extraction import build_invoice_extractor
from backend.invoice_evaluation import evaluate_invoice_run
from backend.invoice_matching import match_invoice
from backend.invoice_models import APDocument, InvoiceEvidence, MatchPolicy
from backend.tests.test_ap_store import ap_store, invoice, receipt


def document():
    return {
        "document_type": "INVOICE", "vendor_name": "Apex Industrial Supply",
        "invoice_number": "INV-901", "po_number": "PO-AP-001", "currency": "USD",
        "lines": [{
            "line_number": 1, "sku": "SS-316 Valve", "description": "Valve",
            "quantity": "2", "unit": "EA", "unit_price": "120", "line_total": "240", "source_page": 1,
        }],
        "freight_amount": "0", "tax_amount": "0", "discount_amount": "0",
        "total_amount": "240", "total_source_page": 1,
    }


def evidence():
    return {
        "orders": [{"is_open": True, "currency": "USD"}],
        "lines": [{
            "qbo_line_id": "1", "sku": "SS-316 Valve", "unit": "EA", "is_active": True,
            "unit_price": Decimal(120), "catalog_price": Decimal(120), "catalog_unit": "EA",
            "ordered_quantity": Decimal(10), "received_quantity": Decimal(6), "allocated_quantity": Decimal(4),
        }],
    }


def test_exact_totals_and_untrusted_numeric_types():
    assert InvoiceEvidence.model_validate(document()).total_amount == 240
    for field, value in (("unit_price", 120.0), ("line_total", "239"), ("quantity", "NaN")):
        invalid = document()
        invalid["lines"][0][field] = value
        with pytest.raises(ValidationError):
            InvoiceEvidence.model_validate(invalid)
    invalid = document()
    invalid["total_amount"] = "241"
    with pytest.raises(ValidationError):
        InvoiceEvidence.model_validate(invalid)


@pytest.mark.parametrize("rate,flags", [("122", []), ("122.01", ["PO_PRICE_VARIANCE", "CONTRACT_PRICE_VARIANCE"])])
def test_price_rounding_boundary(rate, flags):
    payload = document()
    payload["lines"][0].update(unit_price=rate, line_total=str(Decimal(rate) * 2))
    payload["total_amount"] = payload["lines"][0]["line_total"]
    assert [f.code for f in match_invoice(InvoiceEvidence.model_validate(payload), evidence(), MatchPolicy()).findings] == flags


def test_partial_and_cumulative_billing_and_repeated_invoice_sku():
    doc = InvoiceEvidence.model_validate(document())
    assert match_invoice(doc, evidence(), MatchPolicy()).clean
    data = evidence()
    data["lines"][0]["allocated_quantity"] = Decimal(5)
    assert match_invoice(doc, data, MatchPolicy()).findings[0].code == "QUANTITY_EXCEEDS_AVAILABLE"
    repeated = document()
    repeated["lines"].append({**repeated["lines"][0], "line_number": 2})
    repeated["total_amount"] = "480"
    assert match_invoice(InvoiceEvidence.model_validate(repeated), evidence(), MatchPolicy()).findings[0].code == "QUANTITY_EXCEEDS_AVAILABLE"


def test_missing_and_ambiguous_lines_uom_currency_and_freight():
    doc = InvoiceEvidence.model_validate(document())
    data = evidence()
    data["lines"].append(deepcopy(data["lines"][0]))
    assert match_invoice(doc, data, MatchPolicy()).findings[0].code == "SKU_MISSING_OR_AMBIGUOUS"
    data = evidence()
    data["lines"][0]["unit"] = "BOX"
    assert match_invoice(doc, data, MatchPolicy()).findings[0].code == "UOM_MISMATCH"
    payload = document()
    payload.update(freight_amount="10", total_amount="250", currency="EUR")
    assert {f.code for f in match_invoice(InvoiceEvidence.model_validate(payload), evidence(), MatchPolicy()).findings} == {
        "CURRENCY_MISMATCH", "FREIGHT_UNAUTHORIZED",
    }


def test_deepseek_extracts_statement_and_invoice_without_math():
    payloads = iter([{"document": {"document_type": "STATEMENT"}}, {"document": document()}])

    def response(request):
        body = json.loads(request.content)
        assert request.url.host == "api.deepseek.com"
        assert body["response_format"] == {"type": "json_object"}
        assert "Never calculate" in body["messages"][0]["content"]
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(next(payloads)),
            }}],
        })

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            extract = build_invoice_extractor("test-key", client)
            assert (await extract(["statement page"])).document.document_type == "STATEMENT"
            assert (await extract(["invoice page"])).document.total_amount == 240

    asyncio.run(check())


def test_evaluation_persists_and_blocks_statement_without_invoice_registration(ap_store):
    from backend.contracts import IntakeMetadata
    from backend.store import IngestionStore
    from pathlib import Path

    pdf = Path("seed/sample_pdfs/PO-2026-420_Clean.pdf").read_bytes()

    async def extract(_):
        return APDocument(document={"document_type": "STATEMENT"})

    async def check():
        run = await IngestionStore(ap_store.database_url).register(pdf, IntakeMetadata())
        result = await evaluate_invoice_run(ap_store, run.run_id, "123456", extract, MatchPolicy())
        assert result.findings[0].code == "STATEMENT_BLOCKED"

    asyncio.run(check())
    with psycopg.connect(ap_store.database_url) as conn:
        assert conn.execute("SELECT document_type FROM ap_document_evaluations").fetchone() == ("STATEMENT",)
        assert conn.execute("SELECT count(*) FROM vendor_invoices").fetchone() == (0,)


def test_evaluation_reads_receipts_and_previously_billed_quantities(ap_store):
    from backend.contracts import IntakeMetadata
    from backend.store import IngestionStore
    from pathlib import Path

    async def check():
        with psycopg.connect(ap_store.database_url) as conn:
            conn.execute("DELETE FROM qbo_purchase_order_lines WHERE qbo_line_id = '2'")
        await receipt(ap_store, "r1", 6)
        previous = await invoice(ap_store, "OLD", b"old invoice", 4)
        assert await ap_store.reserve_invoice(previous.invoice_id) == "reserved"
        run = await IngestionStore(ap_store.database_url).register(
            Path("seed/sample_pdfs/PO-2026-420_Clean.pdf").read_bytes(), IntakeMetadata(),
        )

        async def extract(_):
            return APDocument(document=document())

        assert (await evaluate_invoice_run(ap_store, run.run_id, "123456", extract, MatchPolicy())).clean
        replay = await evaluate_invoice_run(ap_store, run.run_id, "123456", extract, MatchPolicy())
        assert replay.clean
        reexport = await IngestionStore(ap_store.database_url).register(
            Path("seed/sample_pdfs/PO-2026-420_Clean.pdf").read_bytes() + b"\n", IntakeMetadata(),
        )
        duplicate = await evaluate_invoice_run(ap_store, reexport.run_id, "123456", extract, MatchPolicy())
        assert [f.code for f in duplicate.findings] == ["DUPLICATE"]

    asyncio.run(check())
