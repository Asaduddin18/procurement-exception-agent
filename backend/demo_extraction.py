"""Credit-free extraction results for the exact bundled sample PDFs."""

from functools import cache
from hashlib import sha256

from backend.contracts import ExtractedPurchaseOrder
from backend.extraction import ExtractionError
from seed.generate_pdfs import APEX_CATALOG, APEX_NAME, ORDERS, OUTPUT_DIR


@cache
def fixture_digests() -> dict[bytes, int]:
    return {
        sha256((OUTPUT_DIR / order.filename).read_bytes()).digest(): index
        for index, order in enumerate(ORDERS)
    }


def fixture_order(index: int) -> ExtractedPurchaseOrder:
    order = ORDERS[index]
    return ExtractedPurchaseOrder(
        vendor_name=APEX_NAME, po_number=order.po_number, currency="USD",
        payment_terms=order.terms, freight=0, tax=0, discount=0,
        grand_total=order.total, total_source_page=len(order.pages),
        lines=[
            dict(line_number=line.number, sku=line.sku, description=APEX_CATALOG[line.sku][0],
                 unit=APEX_CATALOG[line.sku][1], quantity=line.quantity,
                 unit_price=line.unit_price, line_total=line.total, source_page=page)
            for page, lines in enumerate(order.pages, 1)
            for line in lines
        ],
    )


def extract_fixture(pdf_bytes: bytes) -> ExtractedPurchaseOrder:
    index = fixture_digests().get(sha256(pdf_bytes).digest())
    if index is None:
        raise ExtractionError("demo_pdf_not_bundled")
    return fixture_order(index)
