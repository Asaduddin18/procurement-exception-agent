"""Public intake contracts and the validated purchase order."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from backend.review_models import ReviewOutcome

CENT = Decimal("0.01")
Region = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
Category = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def exact_decimal_input(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use a decimal string or an integer for exact numbers.")
    return value


Amount = Annotated[
    Decimal, Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False),
    BeforeValidator(exact_decimal_input),
]
UnitPrice = Annotated[
    Decimal, Field(ge=0, max_digits=18, decimal_places=4, allow_inf_nan=False),
    BeforeValidator(exact_decimal_input),
]
Quantity = Annotated[
    Decimal, Field(gt=0, max_digits=12, decimal_places=4, allow_inf_nan=False),
    BeforeValidator(exact_decimal_input),
]


class IntakeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: SourceId | None = None
    category: Category = "procurement"
    region: Region = "US"


class EmailIntake(IntakeMetadata):
    source_id: SourceId
    pdf_base64: str = Field(min_length=1)


class AcceptedRun(BaseModel):
    status: Literal["queued", "duplicate_ignored"]
    run_id: UUID
    thread_id: UUID
    duplicate_count: int = 0


class PurchaseOrderLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_number: int = Field(ge=1, le=999999)
    sku: Label
    description: Label
    quantity: Quantity
    unit: Label
    unit_price: UnitPrice
    line_total: Amount
    source_page: int = Field(ge=1)


class ExtractedPurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_name: Label
    po_number: Label
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    payment_terms: Label
    lines: list[PurchaseOrderLine] = Field(min_length=1, max_length=200)
    freight: Amount
    tax: Amount
    discount: Amount
    grand_total: Amount
    total_source_page: int = Field(ge=1)

    @model_validator(mode="after")
    def reconcile_totals(self) -> Self:
        seen: set[int] = set()
        for line in self.lines:
            if line.line_number in seen:
                raise ValueError(f"Duplicate purchase order line {line.line_number}.")
            seen.add(line.line_number)
            with localcontext(prec=40):
                expected = (line.quantity * line.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
            if expected != line.line_total:
                raise ValueError(f"Printed total does not reconcile for line {line.line_number}.")
        expected_total = sum(line.line_total for line in self.lines) + self.freight + self.tax - self.discount
        if expected_total != self.grand_total:
            raise ValueError("Printed grand total does not reconcile.")
        return self


class RunStatus(BaseModel):
    run_id: UUID
    thread_id: UUID
    status: str
    category: str
    region: str
    error_code: str | None
    extracted_po: ExtractedPurchaseOrder | None
    decision_evidence: ReviewOutcome | None = None
    duplicate_count: int = 0
    trace_url: str | None = None


class RunSummary(BaseModel):
    run_id: UUID
    po_number: str | None
    vendor: str | None
    grand_total: Decimal | None
    currency: str | None
    status: str
    duplicate_count: int
    created_at: datetime
