"""PostgreSQL read boundary for the three procurement MCP tools."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

import psycopg
from psycopg.rows import dict_row

from mcp_server.contracts import CatalogLookup, CatalogPrice, SopCitation, VendorTerms

LOGGER = logging.getLogger(__name__)
SOP_LIMIT = 5

VENDOR_SQL = """
    SELECT vendor_id, vendor_name, payment_terms_days, contract_reference
    FROM public.vendors
    WHERE vendor_name = %s
"""

CATALOG_SQL = """
    SELECT sku, description, currency, unit, unit_price
    FROM public.price_catalog
    WHERE vendor_id = %s AND sku = ANY(%s::text[])
"""

SOP_SQL = """
    WITH eligible AS MATERIALIZED (
        SELECT policy_id, version, status, effective_date, category, region,
               chunk_number, source_location, content, embedding
        FROM public.sop_policy_versions
        JOIN public.sop_policy_chunks USING (policy_id, version)
        WHERE status = 'ACTIVE'
          AND category = %s
          AND region = %s
          AND effective_date <= %s
          AND embedding_space = %s
    )
    SELECT policy_id, version, status, effective_date, category, region,
           chunk_number, source_location, content,
           embedding <=> %s::vector AS cosine_distance
    FROM eligible
    ORDER BY cosine_distance, policy_id, version DESC, chunk_number
    LIMIT %s
"""


class EvidenceUnavailable(RuntimeError):
    """The evidence store could not answer the lookup."""


class EvidenceRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @asynccontextmanager
    async def read(self) -> AsyncIterator[psycopg.AsyncConnection]:
        try:
            async with await psycopg.AsyncConnection.connect(
                self.database_url,
                row_factory=dict_row,
                connect_timeout=3,
                options="-c statement_timeout=5000",
            ) as connection:
                await connection.set_read_only(True)
                yield connection
        except psycopg.Error as exc:
            LOGGER.warning("Procurement evidence read failed (SQLSTATE %s).", exc.sqlstate)
            raise EvidenceUnavailable("Procurement evidence database unavailable.") from None

    async def vendor_terms(self, vendor_name: str) -> VendorTerms | None:
        async with self.read() as connection:
            cursor = await connection.execute(VENDOR_SQL, (vendor_name,))
            row = await cursor.fetchone()
        return None if row is None else VendorTerms.model_validate(row)

    async def approved_prices(self, vendor_name: str, skus: list[str]) -> CatalogLookup:
        requested = list(dict.fromkeys(skus))
        async with self.read() as connection:
            # Use one snapshot so the vendor and prices describe the same committed state.
            await connection.set_isolation_level(psycopg.IsolationLevel.REPEATABLE_READ)
            cursor = await connection.execute(VENDOR_SQL, (vendor_name,))
            row = await cursor.fetchone()
            if row is None:
                return CatalogLookup(
                    status="vendor_not_found", vendor=None, prices=[], missing_skus=requested
                )
            vendor = VendorTerms.model_validate(row)
            cursor = await connection.execute(CATALOG_SQL, (vendor.vendor_id, requested))
            by_sku = {item["sku"]: CatalogPrice.model_validate(item) for item in await cursor.fetchall()}

        prices = [by_sku[sku] for sku in requested if sku in by_sku]
        missing = [sku for sku in requested if sku not in by_sku]
        status = "found" if not missing else "partial" if prices else "sku_not_found"
        return CatalogLookup(status=status, vendor=vendor, prices=prices, missing_skus=missing)

    async def search(
        self, category: str, region: str, as_of: date, embedding_space: str, embedding: str
    ) -> list[SopCitation]:
        async with self.read() as connection:
            cursor = await connection.execute(
                SOP_SQL, (category, region, as_of, embedding_space, embedding, SOP_LIMIT)
            )
            return [SopCitation.model_validate(row) for row in await cursor.fetchall()]
