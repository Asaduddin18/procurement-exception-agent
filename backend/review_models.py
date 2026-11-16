"""Evidence, decision, and human review contracts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from mcp_server.contracts import CatalogPrice, SopCitation, VendorTerms

Role = Literal["PROCUREMENT_REVIEWER", "FINANCE_DIRECTOR"]
Action = Literal["APPROVE", "DISPUTE", "REJECT"]
RuleCode = Literal["PAYMENT_TERMS_DAYS", "FINANCE_DIRECTOR_CAP", "PRICE_VARIANCE_FRACTION"]


class ApprovedRule(BaseModel):
    rule_code: RuleCode
    rule_value: Decimal
    currency: str | None
    policy_id: str
    version: int
    source_chunk_number: int | None


class Evidence(BaseModel):
    evaluated_on: date
    vendor: VendorTerms | None = None
    prices: list[CatalogPrice] = Field(default_factory=list)
    rules: list[ApprovedRule] = Field(default_factory=list)
    citations: list[SopCitation] = Field(default_factory=list)
    issue: str | None = None


class Finding(BaseModel):
    code: str
    line_number: int | None = None
    sku: str | None = None
    observed: str | None = None
    expected: str | None = None
    currency: str | None = None
    variance_percent: Decimal | None = None
    overcharge: Decimal | None = None
    contract_reference: str | None = None
    citation: SopCitation | None = None


class Decision(BaseModel):
    evaluated_on: date
    approval_tier: Role | None = None
    approval_blocked: bool = False
    findings: list[Finding] = Field(default_factory=list)


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    notes: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class Resolution(ResolutionRequest):
    actor_id: str
    role: Role


class ReviewOutcome(BaseModel):
    status: Literal["AWAITING_REVIEW", "APPROVED", "DISPUTED", "REJECTED", "FAILED"]
    decision: Decision | None = None
    resolution: Resolution | None = None
    dispute_draft: str | None = None
    error_code: str | None = None


class AuditEvent(BaseModel):
    event_type: str
    actor_id: str | None
    details: dict
    created_at: datetime
