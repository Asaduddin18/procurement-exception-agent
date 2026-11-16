"""Read exact vendor facts and current versioned policy evidence."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime

import psycopg
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.contracts import ExtractedPurchaseOrder
from backend.review_models import ApprovedRule, Evidence
from backend.risk import REQUIRED_RULES
from backend.store import IngestionStore
from mcp_server.contracts import CatalogLookup, SopSearch, VendorLookup

LOGGER = logging.getLogger(__name__)
SOP_QUERY = "Payment terms, price variance, and Finance Director approval cap"
RULES_SQL = """
WITH eligible AS (
    SELECT DISTINCT ON (policy_id) policy_id, version
    FROM sop_policy_versions
    WHERE status = 'ACTIVE' AND category = %s AND region = %s AND effective_date <= %s
    ORDER BY policy_id, effective_date DESC, version DESC
)
SELECT r.policy_id, r.version, r.rule_code, r.rule_value, r.currency, r.source_chunk_number
FROM eligible JOIN sop_policy_rules r USING (policy_id, version)
WHERE (SELECT count(*) FROM eligible) = 1
ORDER BY r.rule_code
"""


class EvidenceCollector:
    def __init__(
        self, store: IngestionStore, mcp_url: str, clock: Callable[[], date] | None = None
    ) -> None:
        self.store = store
        self.mcp_url = mcp_url
        self.clock = clock or (lambda: datetime.now(UTC).date())

    async def collect(self, order: ExtractedPurchaseOrder, category: str, region: str) -> Evidence:
        now = self.clock()
        try:
            async with asyncio.timeout(35):
                async with streamable_http_client(self.mcp_url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        terms = VendorLookup.model_validate(await self.call(
                            session, "get_vendor_contract_terms", {"vendor_name": order.vendor_name}
                        ))
                        vendor, prices = terms.vendor, []
                        skus = list(dict.fromkeys(line.sku for line in order.lines))
                        for offset in range(0, len(skus), 100):
                            catalog = CatalogLookup.model_validate(await self.call(
                                session, "get_approved_price_catalog",
                                {"vendor_name": order.vendor_name, "sku_list": skus[offset:offset+100]},
                            ))
                            if vendor != catalog.vendor:
                                return Evidence(evaluated_on=now, issue="CONTRACT_EVIDENCE_CHANGED")
                            prices.extend(catalog.prices)
                        return await self.collect_policy(session, category, region, now, vendor, prices)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Streamable HTTP can wrap failures raised inside its context in an ExceptionGroup.
            LOGGER.warning("Procurement evidence collection failed.")
            return Evidence(evaluated_on=now, issue="MCP_EVIDENCE_UNAVAILABLE")

    async def collect_policy(self, session, category, region, now, vendor, prices) -> Evidence:
        evidence = Evidence(evaluated_on=now, vendor=vendor, prices=prices)
        for _ in range(2):
            sop = SopSearch.model_validate(await self.call(
                session, "search_sop_policy", {"query": SOP_QUERY, "category": category, "region": region}
            ))
            if sop.category != category or sop.region != region or sop.evaluated_on != now:
                return Evidence(evaluated_on=now, issue="SOP_SCOPE_MISMATCH")
            evidence.citations = [
                row for row in sop.citations
                if row.category == category and row.region == region and row.effective_date <= now
            ]
            try:
                async with await self.store.connect() as connection:
                    cursor = await connection.execute(RULES_SQL, (category, region, now))
                    evidence.rules = [ApprovedRule.model_validate(row) for row in await cursor.fetchall()]
            except psycopg.Error:
                evidence.issue = "APPROVED_RULES_UNAVAILABLE"
                return evidence

            keys = {(row.policy_id, row.version, row.chunk_number) for row in evidence.citations}
            if set(item.rule_code for item in evidence.rules) == REQUIRED_RULES and all(
                (rule.policy_id, rule.version, rule.source_chunk_number) in keys for rule in evidence.rules
            ):
                break
        return evidence

    @staticmethod
    async def call(session: ClientSession, name: str, arguments: dict) -> dict:
        result = await session.call_tool(name, arguments)
        if result.isError or result.structuredContent is None:
            raise RuntimeError("Evidence tool failed.")
        return result.structuredContent
