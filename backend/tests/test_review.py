import asyncio
import json
import socket
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import psycopg
import pytest
import uvicorn
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.conninfo import make_conninfo

from backend.api import create_app
from backend.contracts import ExtractedPurchaseOrder
from backend.policy_evidence import EvidenceCollector
from backend.review_graph import build_graph
from backend.review_models import Evidence, ReviewOutcome
from backend.review_store import ReviewStore
from backend.review_worker import advance, process_review, setup_checkpointer
from backend.risk import evaluate
from backend.tests.test_extraction import PDF_DIR
from backend.worker import process_one
from mcp_server.embeddings import FixtureEmbedder
from mcp_server.repository import EvidenceRepository
from mcp_server.server import create_server
from seed.generate_pdfs import APEX_CATALOG, APEX_NAME, ORDERS
from seed.seed import bootstrap

AS_OF = date(2026, 10, 19)
CREDENTIALS = json.dumps({
    "buyer-reviewer": {"role": "PROCUREMENT_REVIEWER", "token": "test-reviewer-token"},
    "finance-director": {"role": "FINANCE_DIRECTOR", "token": "test-director-token"},
})
REVIEWER = {"Authorization": "Bearer test-reviewer-token"}
DIRECTOR = {"Authorization": "Bearer test-director-token"}


def fixture_order(index: int) -> ExtractedPurchaseOrder:
    order = ORDERS[index]
    return ExtractedPurchaseOrder(
        vendor_name=APEX_NAME, po_number=order.po_number, currency="USD",
        payment_terms=order.terms, freight=0, tax=0, discount=0,
        grand_total=order.total, total_source_page=len(order.pages),
        lines=[
            dict(line_number=line.number, sku=line.sku, description=APEX_CATALOG[line.sku][0],
                 unit=APEX_CATALOG[line.sku][1], quantity=line.quantity,
                 unit_price=line.unit_price, line_total=line.total, source_page=page)
            for page, lines in enumerate(order.pages, 1) for line in lines
        ],
    )


@asynccontextmanager
async def mcp_endpoint(database_url):
    actor, token = f"phase4_mcp_{uuid4().hex}", "phase4-test-password"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {} IN ROLE procurement_mcp_reader"
        ).format(sql.Identifier(actor), sql.Literal(token)))
    try:
        reader_url = make_conninfo(database_url, user=actor, password=token)
        mcp = create_server(EvidenceRepository(reader_url), FixtureEmbedder(), clock=lambda: AS_OF)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                mcp.streamable_http_app(), log_level="error", ws="none"
            ))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("MCP server exited before startup.")
                        await asyncio.sleep(0.025)
                yield f"http://127.0.0.1:{port}/mcp"
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 10)
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(actor)))


async def upload_and_extract(client, store, index: int) -> str:
    fixture = ORDERS[index]
    pdf = (PDF_DIR / fixture.filename).read_bytes()
    upload = await client.post("/api/v1/ingest", files={"file": (fixture.filename, pdf)})
    assert upload.status_code == 202
    run_id = upload.json()["run_id"]
    duplicate = await client.post("/api/v1/ingest", files={"file": (fixture.filename, pdf)})
    assert duplicate.status_code == 200
    assert duplicate.json()["run_id"] == run_id

    async def structured(pages):
        assert len(pages) == len(fixture.pages)
        return fixture_order(index)

    assert await process_one(store, structured)
    return run_id


async def status(client, run_id):
    response = await client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200
    return response.json()


def test_clean_po_auto_approval_end_to_end(database_url):
    bootstrap(database_url)

    async def check():
        store = ReviewStore(database_url)
        app = create_app(database_url, reviewer_credentials=CREDENTIALS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            run_id = await upload_and_extract(client, store, 1)
            async with mcp_endpoint(database_url) as mcp_url:
                async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
                    await setup_checkpointer(store, saver)
                    collector = EvidenceCollector(store, mcp_url, clock=lambda: AS_OF)
                    assert await process_review(store, build_graph(saver, collector))
            run = await status(client, run_id)
            assert run["status"] == "APPROVED", run
            assert run["decision_evidence"]["decision"]["findings"] == []
            audit = (await client.get(f"/api/v1/runs/{run_id}/audit")).json()
            assert [event["event_type"] for event in audit] == [
                "INTAKE_QUEUED", "PO_EXTRACTED", "PO_APPROVED"
            ]

    asyncio.run(check())


@pytest.mark.parametrize("action,terminal", [
    ("APPROVE", "APPROVED"), ("DISPUTE", "DISPUTED"), ("REJECT", "REJECTED"),
])
def test_checkpoint_restart_and_reviewer_resolution(database_url, action, terminal):
    bootstrap(database_url)

    async def check():
        store = ReviewStore(database_url)
        app = create_app(database_url, reviewer_credentials=CREDENTIALS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            run_id = await upload_and_extract(client, store, 0)
            # Interrupt checkpoint exists, but the worker has not published the pending review.
            async with mcp_endpoint(database_url) as mcp_url:
                async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
                    await setup_checkpointer(store, saver)
                    collector = EvidenceCollector(store, mcp_url, clock=lambda: AS_OF)
                    async with store.claim_review() as job:
                        assert job is not None
                        paused = await advance(build_graph(saver, collector), job)
                        assert paused.status == "AWAITING_REVIEW"
            with psycopg.connect(database_url) as connection:
                connection.execute("""
                    UPDATE procurement_jobs SET lease_expires_at = now() - interval '1 second'
                    WHERE run_id = %s AND kind = 'EVALUATE'
                """, (run_id,))

            class NoMoreEvidenceCalls:
                async def collect(self, *_):
                    pytest.fail("Replay should use persisted checkpoint evidence.")

            async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
                assert await process_review(store, build_graph(saver, NoMoreEvidenceCalls()))
            run = await status(client, run_id)
            assert run["status"] == "AWAITING_REVIEW"
            decision = run["decision_evidence"]["decision"]
            assert decision["approval_tier"] == "FINANCE_DIRECTOR"
            findings = {item["code"]: item for item in decision["findings"]}
            assert set(findings) == {"FINANCE_APPROVAL_REQUIRED", "PAYMENT_TERMS_MISMATCH", "PRICE_VARIANCE"}
            assert all(item["citation"]["version"] == 2026 for item in findings.values())
            price = findings["PRICE_VARIANCE"]
            assert price["line_number"] == 3 and price["sku"] == "SS-316 Valve"
            assert Decimal(price["overcharge"]) == Decimal("1250")
            assert Decimal(price["variance_percent"]) == Decimal("20.83")

            url = f"/api/v1/runs/{run_id}/resolve"
            request = {"action": action, "notes": "Reviewed the contract and evidence."}
            assert (await client.post(url, json=request)).status_code == 401
            forbidden = await client.post(
                url, json={"action": "APPROVE", "notes": "Override"}, headers=REVIEWER
            )
            assert forbidden.status_code == 403
            assert (await client.post(
                url, json=request | {"role": "FINANCE_DIRECTOR"}, headers=REVIEWER
            )).status_code == 422
            chosen_headers = DIRECTOR if action == "APPROVE" else REVIEWER
            first, repeat = await asyncio.gather(*[
                client.post(url, json=request, headers=chosen_headers) for _ in range(2)
            ])
            assert first.status_code == repeat.status_code == 202
            assert (await client.post(
                url, json=request | {"notes": "A different resolution."}, headers=chosen_headers
            )).status_code == 409

            # Also simulate a restart after a resumed terminal checkpoint but before projection.
            async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
                async with store.claim_review() as job:
                    assert job is not None and job.kind == "RESUME"
                    completed = await advance(build_graph(saver, NoMoreEvidenceCalls()), job)
                    assert completed.status == terminal
            with psycopg.connect(database_url) as connection:
                connection.execute("""
                    UPDATE procurement_jobs SET lease_expires_at = now() - interval '1 second'
                    WHERE run_id = %s AND kind = 'RESUME'
                """, (run_id,))
            async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
                graph = build_graph(saver, NoMoreEvidenceCalls())
                assert await process_review(store, graph)
                assert not await process_review(store, graph)
            result = await status(client, run_id)
            assert result["status"] == terminal
            outcome = ReviewOutcome.model_validate(result["decision_evidence"])
            assert outcome.resolution.action == action
            if action == "DISPUTE":
                assert "USD 1250.00" in outcome.dispute_draft
                assert "US-PROCUREMENT v2026, Section 4.1" in outcome.dispute_draft
                assert "CTR-APX-2026-01" in outcome.dispute_draft
            else:
                assert outcome.dispute_draft is None
            audit = (await client.get(f"/api/v1/runs/{run_id}/audit")).json()
            assert [row["event_type"] for row in audit] == [
                "INTAKE_QUEUED", "PO_EXTRACTED", "REVIEW_REQUIRED", "RESOLUTION_QUEUED",
                {"APPROVE": "PO_APPROVED", "DISPUTE": "VENDOR_DISPUTE_DRAFTED", "REJECT": "PO_REJECTED"}[action],
            ]

    asyncio.run(check())


def test_missing_evidence_blocks_approval():
    decision = evaluate(fixture_order(1), Evidence(evaluated_on=AS_OF, issue="MCP_EVIDENCE_UNAVAILABLE"))
    assert decision.approval_blocked
    assert decision.approval_tier == "PROCUREMENT_REVIEWER"
    assert "APPROVED_RULES_UNAVAILABLE" in {item.code for item in decision.findings}


def test_exact_thresholds_do_not_escalate(database_url):
    bootstrap(database_url)

    async def check():
        store = ReviewStore(database_url)
        order = fixture_order(1)
        async with mcp_endpoint(database_url) as url:
            evidence = await EvidenceCollector(store, url, clock=lambda: AS_OF).collect(
                order, "procurement", "US"
            )
        updated = order.model_dump(mode="json")
        updated["lines"][0].update(unit_price="126.00", line_total="2520.00")
        updated.update(freight="5680.00", grand_total="10000.00")
        order = ExtractedPurchaseOrder.model_validate(updated)
        assert evaluate(order, evidence).findings == []

    asyncio.run(check())
