import asyncio
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from backend.api import create_app
from backend.ap_store import APStore
from backend.ap_workflow import build_invoice_graph
from backend.ap_post_worker import process_post
from backend.invoice_models import MatchPolicy
from backend.qbo_posting import PostedBill
from backend.review_store import ReviewStore
from backend.review_worker import process_review, setup_checkpointer
from seed.ap_demo import extract_demo_invoice, seed_receiving
from seed.seed import bootstrap

CREDENTIALS = '{"finance":{"role":"FINANCE_DIRECTOR","token":"finance-token-for-ap-testing"}}'
AUTH = {"Authorization": "Bearer finance-token-for-ap-testing"}


@pytest.fixture
def ap_database(database_url):
    bootstrap(database_url)
    with psycopg.connect(database_url) as conn:
        seed_receiving(conn)
    return database_url


async def upload(client, filename):
    response = await client.post("/api/v1/ingest",
        files={"file": (filename, Path("seed/sample_pdfs", filename).read_bytes(), "application/pdf")},
        data={"document_type": "INVOICE"})
    assert response.status_code == 202
    return UUID(response.json()["run_id"])


async def graph_for(dsn, saver):
    store = ReviewStore(dsn)
    await setup_checkpointer(store, saver)
    return build_invoice_graph(saver, APStore(dsn), "999001", extract_demo_invoice, MatchPolicy())


def test_clean_invoice_enters_posting_outbox_and_syncs_once(ap_database):
    class Posting:
        calls = 0

        async def post_approved_invoice(self, invoice_id):
            self.calls += 1
            with psycopg.connect(ap_database) as conn:
                assert conn.execute("""
                    SELECT count(*) FROM audit_logs WHERE event_type = 'AP_INVOICE_APPROVED'
                """).fetchone() == (1,)
            return PostedBill("bill-test", "pdf-test")

    async def check():
        store = ReviewStore(ap_database)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(ap_database)), base_url="http://test") as client:
            run = await upload(client, "AP_DEMO_Clean.pdf")
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                graph = await graph_for(ap_database, saver)
                assert await process_review(store, None, graph)
            detail = (await client.get(f"/api/v1/runs/{run}")).json()
            assert detail["status"] == "APPROVED"
            assert detail["ap"]["evidence"]["match_snapshot"]["lines"][0]["received_quantity"] == "6"
            duplicate = await client.post("/api/v1/ingest",
                files={"file": ("repeat.pdf", Path("seed/sample_pdfs/AP_DEMO_Clean.pdf").read_bytes())},
                data={"document_type": "INVOICE"})
            assert duplicate.status_code == 200 and duplicate.json()["run_id"] == str(run)
            posting = Posting()
            assert await process_post(APStore(ap_database), posting)
            assert not await process_post(APStore(ap_database), posting)
            assert posting.calls == 1
            assert (await client.get(f"/api/v1/runs/{run}")).json()["status"] == "SYNCED_TO_QBO"

    asyncio.run(check())


@pytest.mark.parametrize("action,terminal", [("APPROVE", "APPROVED"), ("REJECT", "REJECTED"), ("DISPUTE", "DISPUTED")])
def test_invoice_review_survives_restart_and_repeated_resolution(ap_database, action, terminal):
    async def check():
        store = ReviewStore(ap_database)
        app = create_app(ap_database, reviewer_credentials=CREDENTIALS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            run = await upload(client, "AP_DEMO_Price_Exception.pdf")
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            detail = (await client.get(f"/api/v1/runs/{run}")).json()
            assert detail["status"] == "AWAITING_REVIEW"
            assert not detail["decision_evidence"]["decision"]["approval_blocked"]
            url = f"/api/v1/runs/{run}/resolve"
            body = {"action": action, "notes": "Supplier discrepancy reviewed against receiving records."}
            assert (await client.post(url, json=body)).status_code == 401
            assert (await client.post(url, json=body, headers=AUTH)).status_code == 202
            assert (await client.post(url, json=body, headers=AUTH)).status_code == 202
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            result = (await client.get(f"/api/v1/runs/{run}")).json()
            assert result["status"] == terminal
            if action == "DISPUTE":
                assert "AP-PRICE" in result["decision_evidence"]["dispute_draft"]
                assert "SS-316 Valve" in result["decision_evidence"]["dispute_draft"]

    asyncio.run(check())
    with psycopg.connect(ap_database) as conn:
        assert conn.execute("SELECT count(*) FROM procurement_jobs WHERE kind = 'QBO_POST'").fetchone() == (
            1 if action == "APPROVE" else 0,
        )


@pytest.mark.parametrize("filename", ["AP_DEMO_Statement.pdf", "AP_DEMO_Quantity_Exception.pdf"])
def test_statements_and_unreceived_quantities_cannot_be_approved(ap_database, filename):
    async def check():
        store = ReviewStore(ap_database)
        app = create_app(ap_database, reviewer_credentials=CREDENTIALS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            run = await upload(client, filename)
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            result = await client.post(f"/api/v1/runs/{run}/resolve", headers=AUTH,
                json={"action": "APPROVE", "notes": "Attempted override"})
            assert result.status_code == 409

    asyncio.run(check())


def test_changed_quantity_blocks_posting_after_review(ap_database):
    async def check():
        store = ReviewStore(ap_database)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(ap_database, reviewer_credentials=CREDENTIALS)),
            base_url="http://test",
        ) as client:
            run = await upload(client, "AP_DEMO_Price_Exception.pdf")
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            with psycopg.connect(ap_database) as conn:
                conn.execute("UPDATE qbo_purchase_orders SET is_open = false WHERE realm_id = '999001'")
            assert (await client.post(f"/api/v1/runs/{run}/resolve", headers=AUTH,
                json={"action": "APPROVE", "notes": "Reviewed"})).status_code == 202
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            result = (await client.get(f"/api/v1/runs/{run}")).json()
            assert result["status"] == "FAILED" and result["error_code"] == "invoice_evidence_changed"

    asyncio.run(check())


def test_failed_posting_can_be_requeued_by_authenticated_reviewer(ap_database):
    class UnavailablePosting:
        async def post_approved_invoice(self, _):
            raise TimeoutError("Simulated unavailable QBO")

    async def check():
        store = ReviewStore(ap_database)
        app = create_app(ap_database, reviewer_credentials=CREDENTIALS)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            run = await upload(client, "AP_DEMO_Clean.pdf")
            async with AsyncPostgresSaver.from_conn_string(ap_database) as saver:
                assert await process_review(store, None, await graph_for(ap_database, saver))
            for _ in range(3):
                with psycopg.connect(ap_database) as conn:
                    conn.execute("UPDATE procurement_jobs SET available_at = now() WHERE kind = 'QBO_POST'")
                assert await process_post(APStore(ap_database), UnavailablePosting())
            with psycopg.connect(ap_database) as conn:
                assert conn.execute("SELECT status FROM procurement_jobs WHERE kind = 'QBO_POST'").fetchone() == ("FAILED",)
            url = f"/api/v1/runs/{run}/retry-sync"
            assert (await client.post(url)).status_code == 401
            assert (await client.post(url, headers=AUTH)).status_code == 202
            assert (await client.post(url, headers=AUTH)).status_code == 409

    asyncio.run(check())
