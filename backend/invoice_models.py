"""Structured invoice evidence and exact three-way matching contracts."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from backend.contracts import Amount, ExtractedPurchaseOrder, Label, PurchaseOrderLine, UnitPrice, exact_decimal_input


class InvoiceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Literal["INVOICE"]
    vendor_name: Label
    invoice_number: Label
    po_number: Label
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    lines: list[PurchaseOrderLine] = Field(min_length=1, max_length=200)
    freight_amount: Amount
    tax_amount: Amount
    discount_amount: Amount
    total_amount: Amount
    total_source_page: int = Field(ge=1)

    @model_validator(mode="after")
    def reconcile_totals(self) -> Self:
        ExtractedPurchaseOrder(
            vendor_name=self.vendor_name, po_number=self.po_number, currency=self.currency,
            payment_terms="Extraction validation", lines=self.lines,
            freight=self.freight_amount, tax=self.tax_amount, discount=self.discount_amount,
            grand_total=self.total_amount, total_source_page=self.total_source_page,
        )
        return self


class NonInvoiceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Literal["STATEMENT", "OTHER"]


class APDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: Annotated[InvoiceEvidence | NonInvoiceEvidence, Field(discriminator="document_type")]


class MatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    price_fraction: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False), BeforeValidator(exact_decimal_input)] = Decimal("0.01")
    unit_price_absolute: UnitPrice = Decimal("2.00")
    authorized_freight: Amount = Decimal("0")


class MatchFinding(BaseModel):
    code: str
    line_number: int | None = None
    observed: str | None = None
    expected: str | None = None


class MatchResult(BaseModel):
    findings: list[MatchFinding] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.findings
