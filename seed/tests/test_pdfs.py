from decimal import Decimal
from pathlib import Path
import re

from pypdf import PdfReader

from seed.fixtures import APEX, CATALOG
from seed.generate_pdfs import OUTPUT_DIR, generate_pdfs


def read_order(path: Path, page_count: int, terms: str, expected_total: str):
    pages = PdfReader(path).pages
    assert len(pages) == page_count
    items = []
    for page_number, page in enumerate(pages, start=1):
        text = page.extract_text(extraction_mode="layout")
        assert "Apex Industrial Supply" in text
        assert terms in text
        assert f"Page {page_number} of {page_count}" in text
        for match in re.finditer(
            r"^ *([0-9]+) {2,}(.+?) {2,}([0-9]+) {2,}EA {2,}\$([\d,.]+) {2,}\$([\d,.]+)$",
            text,
            flags=re.MULTILINE,
        ):
            line, sku, quantity, price, total = match.groups()
            price = Decimal(price.replace(",", ""))
            total = Decimal(total.replace(",", ""))
            assert Decimal(quantity) * price == total
            items.append((int(line), sku, int(quantity), price, total, page_number))
        if page_number == page_count:
            assert re.search(r"Grand total \(USD\) +\$" + re.escape(expected_total), text)
            assert re.search(r"Freight +\$0\.00", text)
            assert re.search(r"Tax +\$0\.00", text)
    assert sum(line[4] for line in items) == Decimal(expected_total.replace(",", ""))
    assert [line[0] for line in items] == list(range(1, len(items) + 1))
    return items


def test_exception_pdf():
    items = read_order(OUTPUT_DIR / "PO-2026-419_Apex_Overcharge.pdf", 4, "Net-15", "18,450.00")
    assert len(items) == 4
    assert items[2] == (3, "SS-316 Valve", 50, Decimal("145.00"), Decimal("7250.00"), 3)
    catalog = {sku: price for vendor, sku, _, _, _, price in CATALOG if vendor == APEX}
    assert all(amount == catalog[sku] for number, sku, _, amount, _, _ in items if number != 3)
    approved = catalog["SS-316 Valve"]
    overcharge = (items[2][3] - approved) * items[2][2]
    variance = (items[2][3] - approved) / approved * 100
    assert overcharge == Decimal("1250.00")
    assert variance.quantize(Decimal("0.1")) == Decimal("20.8")


def test_clean_pdf():
    items = read_order(OUTPUT_DIR / "PO-2026-420_Clean.pdf", 1, "Net-30", "4,200.00")
    assert len(items) == 3
    catalog = {sku: price for vendor, sku, _, _, _, price in CATALOG if vendor == APEX}
    assert all(amount == catalog[sku] for _, sku, _, amount, _, _ in items)


def test_pdf_generation_is_repeatable(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    generate_pdfs(first)
    generate_pdfs(second)
    for name in ("PO-2026-419_Apex_Overcharge.pdf", "PO-2026-420_Clean.pdf"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
