"""Reference records shared by the seed loader and PO generator."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

APEX = "apex-industrial"
EMBEDDING_DIMENSIONS = 1536
FIXTURE_EMBEDDING_SPACE = "procurement-fixture-v1"

VENDORS = (
    (APEX, "Apex Industrial Supply", 30, "CTR-APX-2026-01"),
    ("harbor-flow", "Harbor Flow Components", 45, "CTR-HFC-2026-01"),
    ("summit-fasteners", "Summit Fastener Works", 30, "CTR-SFW-2026-01"),
)

CATALOG = (
    (APEX, "SS-316 Valve", "316 stainless steel isolation valve", "USD", "EA", Decimal("120.00")),
    (APEX, "GSK-150", "150 mm reinforced flange gasket", "USD", "EA", Decimal("40.00")),
    (APEX, "ACT-24V", "24 V electric actuator assembly", "USD", "EA", Decimal("340.00")),
    (APEX, "CLP-316", "316 stainless steel pipe clamp", "USD", "EA", Decimal("70.00")),
    ("harbor-flow", "SS-316 Valve", "316 stainless steel isolation valve", "USD", "EA", Decimal("128.00")),
    ("summit-fasteners", "BLT-M12", "M12 hex bolt pack", "USD", "PK", Decimal("28.50")),
)


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version: int
    status: str
    effective_date: date
    category: str
    region: str
    title: str


POLICIES = (
    Policy("US-PROCUREMENT", 2026, "ACTIVE", date(2026, 1, 1), "procurement", "US",
           "US Procurement SOP, 2026"),
    Policy("US-PROCUREMENT", 2024, "ARCHIVED", date(2024, 1, 1), "procurement", "US",
           "US Procurement SOP, 2024"),
    Policy("EU-FREIGHT", 2026, "ACTIVE", date(2026, 1, 1), "freight", "EU",
           "EU Freight Procurement SOP"),
    Policy("US-PROCUREMENT", 2027, "ACTIVE", date(2027, 1, 1), "procurement", "US",
           "US Procurement SOP, 2027"),
)

RULES = (
    ("US-PROCUREMENT", 2026, "PAYMENT_TERMS_DAYS", Decimal("30"), None),
    ("US-PROCUREMENT", 2026, "FINANCE_DIRECTOR_CAP", Decimal("10000.00"), "USD"),
    ("US-PROCUREMENT", 2026, "PRICE_VARIANCE_FRACTION", Decimal("0.05"), None),
    ("US-PROCUREMENT", 2024, "FINANCE_DIRECTOR_CAP", Decimal("5000.00"), "USD"),
    ("EU-FREIGHT", 2026, "FINANCE_DIRECTOR_CAP", Decimal("7500.00"), "EUR"),
    ("US-PROCUREMENT", 2027, "FINANCE_DIRECTOR_CAP", Decimal("12500.00"), "USD"),
)

RULE_SOURCES = (
    ("US-PROCUREMENT", 2026, "PAYMENT_TERMS_DAYS", 1),
    ("US-PROCUREMENT", 2026, "FINANCE_DIRECTOR_CAP", 2),
    ("US-PROCUREMENT", 2026, "PRICE_VARIANCE_FRACTION", 3),
    ("US-PROCUREMENT", 2024, "FINANCE_DIRECTOR_CAP", 1),
    ("EU-FREIGHT", 2026, "FINANCE_DIRECTOR_CAP", 1),
    ("US-PROCUREMENT", 2027, "FINANCE_DIRECTOR_CAP", 1),
)

# The first three vector dimensions represent terms, approval caps, and variance.
# Close negative controls let the retrieval tests expose missing SQL filters.
CHUNKS = (
    ("US-PROCUREMENT", 2026, 1, "Section 2.1, payment terms",
     "Standard payment terms for US procurement are Net-30.", (1, 0, 0)),
    ("US-PROCUREMENT", 2026, 2, "Section 3.2, approval authority",
     "US purchase orders above USD 10,000 require Finance Director approval.", (0, 0.99, 0.1)),
    ("US-PROCUREMENT", 2026, 3, "Section 4.1, price variance",
     "Flag an item when the quoted unit price is more than 5% above the approved catalog.",
     (0, 0, 1)),
    ("US-PROCUREMENT", 2024, 1, "Section 3.1, approval authority",
     "US purchase orders above USD 5,000 require Finance Director approval.", (0, 1, 0)),
    ("EU-FREIGHT", 2026, 1, "Section 4, freight approval",
     "EU freight orders above EUR 7,500 require Finance Director approval.", (0, 1, 0)),
    ("US-PROCUREMENT", 2027, 1, "Section 3.2, approval authority",
     "US purchase orders above USD 12,500 require Finance Director approval.", (0, 1, 0)),
)
