"""Generate deterministic synthetic purchase orders for procurement verification."""

import argparse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from reportlab.lib.colors import Color, HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from seed.fixtures import APEX, CATALOG, VENDORS

OUTPUT_DIR = Path(__file__).with_name("sample_pdfs")
APEX_NAME = next(name for vendor_id, name, *_ in VENDORS if vendor_id == APEX)
APEX_CATALOG = {
    sku: (description, unit, price)
    for vendor_id, sku, description, _, unit, price in CATALOG
    if vendor_id == APEX
}
NAVY = HexColor("#12273D")
SLATE = HexColor("#46576A")
RULE = HexColor("#D2DBE5")
PALE = HexColor("#EAF0F6")


@dataclass(frozen=True)
class Line:
    number: int
    sku: str
    quantity: int
    quoted_unit_price: Decimal | None = None

    @property
    def unit_price(self) -> Decimal:
        price = self.quoted_unit_price
        return APEX_CATALOG[self.sku][2] if price is None else price

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class PurchaseOrder:
    filename: str
    po_number: str
    terms: str
    pages: tuple[tuple[Line, ...], ...]
    total: Decimal


ORDERS = (
    PurchaseOrder(
        "PO-2026-419_Apex_Overcharge.pdf", "PO-2026-419", "Net-15",
        ((Line(1, "GSK-150", 40),), (Line(2, "ACT-24V", 20),),
         (Line(3, "SS-316 Valve", 50, Decimal("145.00")),), (Line(4, "CLP-316", 40),)),
        Decimal("18450.00"),
    ),
    PurchaseOrder(
        "PO-2026-420_Clean.pdf", "PO-2026-420", "Net-30",
        ((Line(1, "SS-316 Valve", 20), Line(2, "GSK-150", 10), Line(3, "CLP-316", 20)),),
        Decimal("4200.00"),
    ),
)

PAGE_SECTIONS = (
    ("Order and commercial terms",
     "Reference this purchase order on packing lists and billing records."),
    ("Electrical components", "Ship actuator assemblies with wiring diagrams and serial numbers."),
    ("Process equipment", "Include the mill certificate and pressure test record for each lot."),
    ("Receiving and order totals", "Label each carton with the purchase order and applicable line number."),
)


def money(amount: Decimal) -> str:
    return f"${amount:,.2f}"


def draw_text(
    pdf: Canvas, x: int, y: int, value: str, *, size: int = 10, bold: bool = False,
    color: Color = NAVY, right: bool = False,
) -> None:
    pdf.setFillColor(color)
    pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    if right:
        pdf.drawRightString(x, y, value)
    else:
        pdf.drawString(x, y, value)


def draw_page(
    pdf: Canvas, order: PurchaseOrder, page: int, lines: tuple[Line, ...]
) -> None:
    page_count = len(order.pages)
    pdf.setFillColor(NAVY)
    pdf.rect(0, 657, 612, 135, fill=1, stroke=0)
    draw_text(pdf, 48, 749, "NORTHSTAR PROCESS SYSTEMS", size=11, bold=True, color=white)
    draw_text(pdf, 48, 703, "Purchase Order", size=31, bold=True, color=white)
    draw_text(pdf, 564, 705, order.po_number, size=15, bold=True, color=white, right=True)

    for x, label, first, second in (
        (48, "SUPPLIER", APEX_NAME, "Vendor reference: APX-001"),
        (312, "SHIP TO", "Northstar Process Systems", "Receiving Dock B, Houston, TX"),
    ):
        draw_text(pdf, x, 622, label, size=9, bold=True, color=SLATE)
        draw_text(pdf, x, 600, first, size=13, bold=True)
        draw_text(pdf, x, 581, second, color=SLATE)

    for x, label, value in (
        (48, "ORDER DATE", "2026-10-19"),
        (182, "PAYMENT TERMS", order.terms),
        (312, "CURRENCY", "USD"),
        (449, "DELIVER BY", "2026-11-02"),
    ):
        draw_text(pdf, x, 540, label, size=9, bold=True, color=SLATE)
        draw_text(pdf, x, 520, value, size=12, bold=True)

    heading, instruction = PAGE_SECTIONS[page - 1]
    draw_text(pdf, 48, 463, heading, size=16, bold=True)
    pdf.setFillColor(PALE)
    pdf.rect(48, 416, 516, 26, fill=1, stroke=0)
    for x, label, right in (
        (56, "LINE", False), (94, "SKU / DESCRIPTION", False), (363, "QTY", True),
        (409, "UNIT", True), (484, "UNIT PRICE", True), (554, "LINE TOTAL", True),
    ):
        draw_text(pdf, x, 425, label, size=8, bold=True, right=right)

    y = 393
    for line in lines:
        description, unit, _ = APEX_CATALOG[line.sku]
        for x, value, right in (
            (56, str(line.number), False), (94, line.sku, False), (363, str(line.quantity), True),
            (409, unit, True), (484, money(line.unit_price), True), (554, money(line.total), True),
        ):
            draw_text(pdf, x, y, value, bold=x == 94, right=right)
        draw_text(pdf, 94, y - 15, description, size=9, color=SLATE)
        pdf.setStrokeColor(RULE)
        pdf.line(48, y - 26, 564, y - 26)
        y -= 47

    if page == page_count:
        for y, label, amount in (
            (223, "Subtotal", order.total),
            (205, "Freight", Decimal("0.00")),
            (187, "Tax", Decimal("0.00")),
            (158, "Grand total (USD)", order.total),
        ):
            draw_text(pdf, 360, y, label, bold=y == 158)
            draw_text(pdf, 554, y, money(amount), bold=y == 158, right=True)

    draw_text(pdf, 48, 112, "RECEIVING INSTRUCTIONS", size=9, bold=True)
    draw_text(pdf, 48, 94, instruction, size=9, color=SLATE)
    pdf.setStrokeColor(RULE)
    pdf.line(48, 64, 564, 64)
    draw_text(pdf, 48, 46, "SYNTHETIC PROCUREMENT FIXTURE", size=8, color=SLATE)
    draw_text(pdf, 564, 46, f"Page {page} of {page_count}", size=9, color=SLATE, right=True)
    pdf.showPage()


def generate_pdfs(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for order in ORDERS:
        total = sum((line.total for page in order.pages for line in page), Decimal("0.00"))
        if total != order.total:
            raise ValueError(f"{order.po_number} totals {total}, expected {order.total}")
        pdf = Canvas(str(output_dir / order.filename), pagesize=letter, invariant=True)
        pdf.setTitle(order.po_number)
        pdf.setAuthor("Northstar Process Systems")
        for page, lines in enumerate(order.pages, start=1):
            draw_page(pdf, order, page, lines)
        pdf.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    generate_pdfs(parser.parse_args().output_dir)


if __name__ == "__main__":
    main()
