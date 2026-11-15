import asyncio
from datetime import date
import os
import re
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from mcp_server.embeddings import FixtureEmbedder
from mcp_server.server import create_server


async def verify_tools(session: ClientSession) -> None:
    await session.initialize()
    tools = (await session.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "get_vendor_contract_terms", "get_approved_price_catalog", "search_sop_policy"
    }
    assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)

    terms = await session.call_tool("get_vendor_contract_terms", {
        "vendor_name": "Apex Industrial Supply"
    })
    assert not terms.isError
    assert terms.structuredContent["vendor"]["payment_terms_days"] == 30

    prices = await session.call_tool("get_approved_price_catalog", {
        "vendor_name": "Apex Industrial Supply", "sku_list": ["SS-316 Valve", "unknown"]
    })
    assert not prices.isError
    assert prices.structuredContent["prices"][0]["unit_price"] == "120.0000"
    assert prices.structuredContent["missing_skus"] == ["unknown"]

    search = await session.call_tool("search_sop_policy", {
        "query": "Finance Director approval cap", "category": "PROCUREMENT", "region": "us"
    })
    assert not search.isError
    assert search.structuredContent["category"] == "procurement"
    assert search.structuredContent["region"] == "US"
    citations = search.structuredContent["citations"]
    assert citations
    assert all(
        (row["policy_id"], row["status"], row["region"], row["category"])
        == ("US-PROCUREMENT", "ACTIVE", "US", "procurement")
        and row["version"] != 2024
        and row["effective_date"] <= search.structuredContent["evaluated_on"]
        for row in citations
    )

    empty = await session.call_tool("search_sop_policy", {
        "query": "Approval", "category": "manufacturing"
    })
    assert not empty.isError
    assert empty.structuredContent["status"] == "no_matches"
    bad = await session.call_tool("search_sop_policy", {"query": "Approval", "region": ""})
    assert bad.isError


def test_stdio_transport(reader_url):
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            env={"MCP_DATABASE_URL": reader_url, "MCP_EMBEDDING_PROVIDER": "fixture"},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await verify_tools(session)

    asyncio.run(check())


def test_streamable_http_transport(reader_url):
    async def check():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "mcp_server.server", "--transport", "streamable-http", "--port", "0",
            env={**os.environ, "MCP_DATABASE_URL": reader_url, "MCP_EMBEDDING_PROVIDER": "fixture"},
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(15):
                while line := await process.stderr.readline():
                    if match := re.search(rb"http://127\.0\.0\.1:(\d+)", line):
                        port = int(match.group(1))
                        break
                else:
                    raise AssertionError("HTTP MCP server exited before binding a port.")
            async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (
                read, write, _,
            ):
                async with ClientSession(read, write) as session:
                    await verify_tools(session)
        finally:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)

    asyncio.run(check())


def test_server_normalizes_scope_and_uses_injected_date():
    class MissingPolicyRepository:
        async def search(self, category, region, evaluated_on, space, embedding):
            assert (category, region, evaluated_on, space) == (
                "procurement", "US", date(2026, 10, 19), FixtureEmbedder.space
            )
            return []

    async def check():
        server = create_server(MissingPolicyRepository(), FixtureEmbedder(), lambda: date(2026, 10, 19))
        _, response = await server.call_tool("search_sop_policy", {"query": "Approval cap"})
        assert response["status"] == "no_matches"
        assert response["evaluated_on"] == "2026-10-19"

    asyncio.run(check())
