"""Repeatable offline AP receiving records and explicitly labeled document fixtures."""

from pathlib import Path

import pymupdf
from psycopg import Connection

from backend.extraction import ExtractionError
from backend.invoice_models import APDocument

DEMO_REALM = "999001"
FIXTURES = {
    "AP_DEMO_Clean.pdf": ("CLEAN", "2", "120", "240"),
    "AP_DEMO_Price_Exception.pdf": ("PRICE", "1", "145", "145"),
    "AP_DEMO_Quantity_Exception.pdf": ("QUANTITY", "8", "120", "960"),
    "AP_DEMO_Statement.pdf": ("STATEMENT", "0", "0", "0"),
}


def fixture_text(name):
    code, quantity, price, total = FIXTURES[name]
    return (
        f"OFFLINE AP DEMO {code}\nApex Industrial Supply\nInvoice AP-{code}\nPO AP-DEMO-RECEIVING\n"
        f"SS-316 Valve | EA | Quantity {quantity} | Unit price USD {price}\nTotal USD {total}\n"
    )


def generate():
    for name in FIXTURES:
        with pymupdf.open() as doc:
            doc.new_page().insert_text((60, 72), fixture_text(name), fontsize=11)
            doc.save(Path("seed/sample_pdfs") / name)


async def extract_demo_invoice(pages):
    name = next((name for name in FIXTURES if pages == [fixture_text(name).strip()]), None)
    if name is None:
        raise ExtractionError("invoice_fixture_not_recognized")
    code, quantity, price, total = FIXTURES[name]
    if code == "STATEMENT":
        return APDocument(document={"document_type": "STATEMENT"})
    return APDocument(document={
        "document_type": "INVOICE", "vendor_name": "Apex Industrial Supply",
        "invoice_number": f"AP-{code}", "po_number": "AP-DEMO-RECEIVING", "currency": "USD",
        "lines": [{
            "line_number": 1, "sku": "SS-316 Valve", "description": "Stainless steel valve",
            "quantity": quantity, "unit": "EA", "unit_price": price, "line_total": total, "source_page": 1,
        }],
        "freight_amount": "0", "tax_amount": "0", "discount_amount": "0",
        "total_amount": total, "total_source_page": 1,
    })


def seed_receiving(connection: Connection):
    connection.execute("""
        INSERT INTO qbo_purchase_orders VALUES
            ('999001', 'demo-po', 'AP-DEMO-RECEIVING', 'apex-industrial', 'demo-vendor', 'USD', true, now())
        ON CONFLICT DO NOTHING
    """)
    connection.execute("""
        INSERT INTO qbo_purchase_order_lines
            (realm_id, qbo_po_id, qbo_line_id, qbo_item_id, sku, unit, ordered_quantity, unit_price)
        VALUES ('999001', 'demo-po', '1', 'demo-item', 'SS-316 Valve', 'EA', 10, 120)
        ON CONFLICT DO NOTHING
    """)
    connection.execute("""
        INSERT INTO goods_receipts VALUES
            ('999001', 'demo-warehouse', 'demo-receipt', 'demo-po', '1', 6, '2026-11-18T00:00:00Z')
        ON CONFLICT DO NOTHING
    """)


if __name__ == "__main__":
    generate()
