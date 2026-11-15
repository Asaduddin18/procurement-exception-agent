"""Serve three read-only procurement evidence tools over MCP."""

import argparse
import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

from mcp_server.contracts import CatalogLookup, SopSearch, VendorLookup
from mcp_server.embeddings import Embedder, FixtureEmbedder, OpenAIEmbedder, vector_parameter
from mcp_server.repository import EvidenceRepository

VendorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Sku = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
SkuList = Annotated[list[Sku], Field(min_length=1, max_length=100)]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Category = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
]
Region = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z]{2}$")]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def utc_date() -> date:
    return datetime.now(UTC).date()


def create_server(
    repository: EvidenceRepository, embedder: Embedder, clock: Callable[[], date] = utc_date
) -> FastMCP:
    server = FastMCP(
        "procurement-evidence", host="127.0.0.1", json_response=True, stateless_http=True
    )

    @server.tool(annotations=READ_ONLY)
    async def get_vendor_contract_terms(vendor_name: VendorName) -> VendorLookup:
        """Return contract terms and a contract reference for an exact vendor name."""
        vendor = await repository.vendor_terms(vendor_name)
        return VendorLookup(status="vendor_not_found" if vendor is None else "found", vendor=vendor)

    @server.tool(annotations=READ_ONLY)
    async def get_approved_price_catalog(vendor_name: VendorName, sku_list: SkuList) -> CatalogLookup:
        """Return exact prices for the vendor's requested SKUs, including missing identifiers."""
        return await repository.approved_prices(vendor_name, sku_list)

    @server.tool(annotations=READ_ONLY)
    async def search_sop_policy(
        query: Query, category: Category | None = None, region: Region = "US"
    ) -> SopSearch:
        """Return applicable SOP excerpts with citations for the specified category and region."""
        effective_category = "procurement" if category is None else category.lower()
        effective_region = region.upper()
        evaluated_on = clock()
        embedding = vector_parameter(await embedder.embed(query))
        citations = await repository.search(
            effective_category, effective_region, evaluated_on, embedder.space, embedding
        )
        return SopSearch(
            status="found" if citations else "no_matches",
            category=effective_category,
            region=effective_region,
            evaluated_on=evaluated_on,
            citations=citations,
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--port", type=int, default=8001, help="Loopback Streamable HTTP port.")
    args = parser.parse_args()

    database_url = os.environ.get("MCP_DATABASE_URL")
    if not database_url:
        parser.error("MCP_DATABASE_URL must identify the read-only procurement database login.")

    provider = os.environ.get("MCP_EMBEDDING_PROVIDER", "fixture")
    embedder: Embedder
    if provider == "fixture":
        embedder = FixtureEmbedder()
    elif provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            parser.error("OPENAI_API_KEY is required when MCP_EMBEDDING_PROVIDER=openai.")
        embedder = OpenAIEmbedder(api_key)
    else:
        parser.error("MCP_EMBEDDING_PROVIDER must be fixture or openai.")

    server = create_server(EvidenceRepository(database_url), embedder)
    server.settings.port = args.port
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
