"""Contract checks for the QBO setup manifest."""

from decimal import Decimal

from seed.fixtures import APEX, CATALOG
from seed.qbo_sandbox import sandbox_manifest


def test_sandbox_manifest_matches_apex_catalog():
    manifest = sandbox_manifest()
    catalog = {sku: (unit, price) for vendor, sku, _, _, unit, price in CATALOG if vendor == APEX}

    assert manifest["vendor"] == "Apex Industrial Supply"
    assert manifest["currency"] == "USD"
    assert manifest["payment_terms_days"] == 30
    orders = {order["po_number"]: order for order in manifest["purchase_orders"]}
    assert len(orders) == len(manifest["purchase_orders"]) == 2
    assert {number: order["total"] for number, order in orders.items()} == {
        "AP-DEMO-001": "4200.00",
        "AP-DEMO-002": "9400.00",
    }
    for order in orders.values():
        assert sum(Decimal(line["amount"]) for line in order["lines"]) == Decimal(order["total"])
        for line in order["lines"]:
            unit, price = catalog[line["sku"]]
            assert line["unit"] == unit
            assert Decimal(line["unit_price"]) == price
            assert Decimal(line["amount"]) == price * line["quantity"]
