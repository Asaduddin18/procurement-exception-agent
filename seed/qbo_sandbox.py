"""Print the catalog-backed purchase orders for QuickBooks Online sandbox setup."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from seed.fixtures import APEX, CATALOG, VENDORS


@dataclass(frozen=True)
class SandboxLine:
    sku: str
    description: str
    unit: str
    quantity: int
    unit_price: Decimal

    @property
    def amount(self) -> Decimal:
        return self.quantity * self.unit_price


def sandbox_manifest() -> dict:
    _, vendor_name, payment_terms_days, _ = next(vendor for vendor in VENDORS if vendor[0] == APEX)
    catalog = {entry[1]: entry[2:] for entry in CATALOG if entry[0] == APEX}
    orders = []
    for po_number, items in (
        ("AP-DEMO-001", (("SS-316 Valve", 20), ("GSK-150", 10), ("CLP-316", 20))),
        ("AP-DEMO-002", (("SS-316 Valve", 50), ("ACT-24V", 10))),
    ):
        lines = []
        for sku, quantity in items:
            description, currency, unit, unit_price = catalog[sku]
            if currency != "USD":
                raise ValueError(f"US sandbox item {sku} must be priced in USD")
            lines.append(SandboxLine(sku, description, unit, quantity, unit_price))
        orders.append({
            "po_number": po_number,
            "lines": [
                {**asdict(line), "unit_price": str(line.unit_price), "amount": str(line.amount)}
                for line in lines
            ],
            "total": str(sum(line.amount for line in lines)),
        })
    return {
        "vendor": vendor_name,
        "payment_terms_days": payment_terms_days,
        "currency": "USD",
        "purchase_orders": orders,
    }


if __name__ == "__main__":
    print(json.dumps(sandbox_manifest(), indent=2))
