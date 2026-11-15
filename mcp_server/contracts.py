"""Typed MCP response contracts."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class VendorTerms(BaseModel):
    vendor_id: str
    vendor_name: str
    payment_terms_days: int
    contract_reference: str


class VendorLookup(BaseModel):
    status: Literal["found", "vendor_not_found"]
    vendor: VendorTerms | None


class CatalogPrice(BaseModel):
    sku: str
    description: str
    currency: str
    unit: str
    unit_price: Decimal


class CatalogLookup(BaseModel):
    status: Literal["found", "partial", "sku_not_found", "vendor_not_found"]
    vendor: VendorTerms | None
    prices: list[CatalogPrice]
    missing_skus: list[str]


class SopCitation(BaseModel):
    policy_id: str
    version: int
    status: Literal["ACTIVE"]
    effective_date: date
    category: str
    region: str
    chunk_number: int
    source_location: str
    content: str
    cosine_distance: float


class SopSearch(BaseModel):
    status: Literal["found", "no_matches"]
    category: str
    region: str
    evaluated_on: date
    citations: list[SopCitation]
