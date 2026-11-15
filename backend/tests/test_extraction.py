import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from backend.contracts import ExtractedPurchaseOrder
from backend.extraction import ExtractionError, build_extractor, parse_pdf

PDF_DIR = Path(__file__).resolve().parents[2] / "seed" / "sample_pdfs"


def example_order() -> dict:
    return {
        "vendor_name": "Example Supply",
        "po_number": "PO-001",
        "currency": "USD",
        "payment_terms": "Net 30",
        "lines": [{
            "line_number": 1, "sku": "VALVE", "description": "Valve", "quantity": "3",
            "unit": "EA", "unit_price": "1.335", "line_total": "4.01", "source_page": 1,
        }],
        "freight": "0.50", "tax": "0.75", "discount": "0.25", "grand_total": "5.01",
        "total_source_page": 1,
    }


def test_decimal_validation_reconciles_rounded_lines_and_printed_adjustments():
    order = ExtractedPurchaseOrder.model_validate(example_order())
    assert order.grand_total == Decimal("5.01")
    assert order.lines[0].unit_price == Decimal("1.335")


@pytest.mark.parametrize("field,value", [("line_total", "4.00"), ("unit_price", 1.335)])
def test_invalid_line_evidence_is_rejected(field, value):
    payload = example_order()
    payload["lines"][0][field] = value
    with pytest.raises(ValidationError):
        ExtractedPurchaseOrder.model_validate(payload)


def test_grand_total_and_duplicate_lines_are_rejected():
    payload = example_order()
    payload["grand_total"] = "5.02"
    with pytest.raises(ValidationError, match="grand total"):
        ExtractedPurchaseOrder.model_validate(payload)
    payload["lines"] *= 2
    with pytest.raises(ValidationError, match="Duplicate"):
        ExtractedPurchaseOrder.model_validate(payload)


def test_pdf_subprocess_extracts_all_pages_in_order():
    pages = asyncio.run(parse_pdf((PDF_DIR / "PO-2026-419_Apex_Overcharge.pdf").read_bytes()))
    assert len(pages) == 4
    assert "SS-316 Valve" in pages[2] and "$145.00" in pages[2]
    assert "$18,450.00" in pages[3]
    with pytest.raises(ExtractionError, match="pdf_unreadable"):
        asyncio.run(parse_pdf(b"%PDF-1.7\ncorrupt"))


def test_model_request_uses_deepseek_and_retries_transient_errors():
    responses = iter((429, 503, 200))
    requests = []

    def mock(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer test-deepseek-key"
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["response_format"] == {"type": "json_object"}
        assert json.loads(payload["messages"][1]["content"])[1]["page"] == 2
        status = next(responses)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Transient failure"}})
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(example_order()),
            }}],
        })

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            order = await build_extractor("test-deepseek-key", client)(["page 1", "page 2"])
            assert order.grand_total == Decimal("5.01")

    asyncio.run(verify())
    assert len(requests) == 3


def test_schema_mismatch_and_authentication_errors_do_not_retry():
    calls = []

    def mock(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(401, json={"error": {"message": "Invalid credential"}})
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": "{}",
            }}],
        })

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
            extract = build_extractor("test-deepseek-key", client)
            with pytest.raises(ExtractionError, match="invalid_structured_output"):
                await extract(["text"])
            assert len(calls) == 1
            with pytest.raises(ExtractionError, match="extraction_provider_failed"):
                await extract(["text"])

    asyncio.run(verify())
    assert len(calls) == 2
