import asyncio
from base64 import b64encode
from decimal import Decimal
from uuid import UUID

import httpx
import psycopg
import pytest

from backend.api import MAX_REQUEST_BYTES, create_app
from backend.contracts import ExtractedPurchaseOrder, IntakeMetadata
from backend.extraction import ExtractionError
from backend.store import MAX_JOB_ATTEMPTS, IngestionStore
from backend.tests.test_extraction import PDF_DIR, example_order
from backend.worker import process_one
from seed.generate_pdfs import APEX_CATALOG, APEX_NAME, ORDERS
from seed.seed import bootstrap


@pytest.fixture
def store(database_url):
    bootstrap(database_url, migrate_only=True)
    return IngestionStore(database_url)


def test_concurrent_intake_and_email_forward_share_a_single_committed_run(store):
    pdf = (PDF_DIR / "PO-2026-419_Apex_Overcharge.pdf").read_bytes()

    async def verify():
        app = create_app(store.database_url, webhook_token="test-token")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            multipart = client.post("/api/v1/ingest", files={"file": ("a.pdf", pdf, "application/pdf")})
            email = client.post("/api/v1/ingest", headers={"Authorization": "Bearer test-token"}, json={
                "source_id": "email-message-123", "pdf_base64": b64encode(pdf).decode(),
            })
            responses = await asyncio.gather(multipart, email)
            assert sorted(response.status_code for response in responses) == [200, 202]
            for response in responses:
                expected = "queued" if response.status_code == 202 else "duplicate_ignored"
                assert response.json()["status"] == expected
            first, second = (response.json() for response in responses)
            assert first["run_id"] == second["run_id"]
            assert first["thread_id"] == second["thread_id"]
            status = await client.get(f"/api/v1/runs/{first['run_id']}")
            assert status.json()["status"] == "QUEUED"

    asyncio.run(verify())
    with psycopg.connect(store.database_url) as connection:
        assert connection.execute("""
            SELECT (SELECT count(*) FROM procurement_runs),
                   (SELECT count(*) FROM processed_events),
                   (SELECT count(*) FROM procurement_inputs),
                   (SELECT count(*) FROM procurement_jobs),
                   (SELECT count(*) FROM audit_logs)
        """).fetchone() == (1, 1, 1, 1, 1)


def test_failed_intake_rolls_back_run_digest_and_payload(store):
    with psycopg.connect(store.database_url) as connection:
        connection.execute("""
            ALTER TABLE procurement_jobs ADD CONSTRAINT force_failure CHECK (kind = 'RESUME')
        """)
    pdf = (PDF_DIR / "PO-2026-419_Apex_Overcharge.pdf").read_bytes()
    with pytest.raises(psycopg.errors.CheckViolation):
        asyncio.run(store.register(pdf, IntakeMetadata()))

    with psycopg.connect(store.database_url) as connection:
        assert connection.execute("""
            SELECT (SELECT count(*) FROM procurement_runs),
                   (SELECT count(*) FROM processed_events),
                   (SELECT count(*) FROM procurement_inputs)
        """).fetchone() == (0, 0, 0)


def test_worker_recovers_expired_lease_and_rejects_stale_write(store):
    pdf = (PDF_DIR / "PO-2026-420_Clean.pdf").read_bytes()
    received = asyncio.run(store.register(pdf, IntakeMetadata()))
    original_job = asyncio.run(store.claim())
    assert original_job is not None
    assert asyncio.run(store.claim()) is None
    with psycopg.connect(store.database_url) as connection:
        connection.execute("""
            UPDATE procurement_jobs SET lease_expires_at = now() - interval '1 second'
            WHERE job_id = %s
        """, (original_job.job_id,))
    assert not asyncio.run(store.finish(original_job, error_code="expired_worker"))

    async def extract(pages):
        assert len(pages) == 1 and "PO-2026-420" in pages[0]
        fixture = ORDERS[1]
        payload = example_order() | {
            "vendor_name": APEX_NAME, "po_number": fixture.po_number,
            "payment_terms": fixture.terms, "grand_total": str(fixture.total),
            "freight": "0", "tax": "0", "discount": "0",
        }
        payload["lines"] = [
            {
                "line_number": line.number, "sku": line.sku, "description": APEX_CATALOG[line.sku][0],
                "quantity": line.quantity, "unit": APEX_CATALOG[line.sku][1],
                "unit_price": str(line.unit_price), "line_total": str(line.total), "source_page": 1,
            }
            for line in fixture.pages[0]
        ]
        return ExtractedPurchaseOrder.model_validate(payload)

    async def verify():
        restarted_worker_store = IngestionStore(store.database_url)
        assert await process_one(restarted_worker_store, extract)
        assert not await restarted_worker_store.finish(original_job, error_code="stale_worker")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app(store.database_url)), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/v1/runs/{received.run_id}")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "EXTRACTED"
        assert Decimal(result["extracted_po"]["grand_total"]) == Decimal("4200.00")
        assert result["error_code"] is None

    asyncio.run(verify())
    with psycopg.connect(store.database_url) as connection:
        assert connection.execute("""
            SELECT status, attempts, lease_token FROM procurement_jobs WHERE run_id = %s
        """, (received.run_id,)).fetchone() == ("COMPLETED", 2, None)
        assert connection.execute("""
            SELECT event_type FROM audit_logs WHERE run_id = %s ORDER BY audit_id
        """, (received.run_id,)).fetchall() == [("INTAKE_QUEUED",), ("PO_EXTRACTED",)]


def test_worker_records_failure_and_does_not_run_exhausted_jobs(store):
    pdf = (PDF_DIR / "PO-2026-420_Clean.pdf").read_bytes()

    async def fail_extraction(_):
        raise ExtractionError("invalid_structured_output")

    first = asyncio.run(store.register(pdf, IntakeMetadata()))
    assert asyncio.run(process_one(store, fail_extraction))
    failed = asyncio.run(store.get_run(first.run_id))
    assert failed is not None and failed.status == "FAILED"
    assert failed.error_code == "invalid_structured_output"
    assert failed.extracted_po is None

    second = asyncio.run(store.register(pdf + b"\n", IntakeMetadata()))
    original_job = asyncio.run(store.claim())
    assert original_job is not None
    with psycopg.connect(store.database_url) as connection:
        connection.execute("""
            UPDATE procurement_jobs
            SET attempts = %s, lease_expires_at = now() - interval '1 second'
            WHERE job_id = %s
        """, (MAX_JOB_ATTEMPTS, original_job.job_id))

    async def unexpected(_):
        pytest.fail("Exhausted jobs must not reach the extractor")

    assert asyncio.run(process_one(store, unexpected))
    exhausted = asyncio.run(store.get_run(second.run_id))
    assert exhausted is not None and exhausted.error_code == "job_attempts_exhausted"


def test_bad_intake_is_rejected_before_storage():
    app = create_app(webhook_token="test-token")

    async def verify():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await client.post("/api/v1/ingest", content=b"bad")).status_code == 415
            assert (await client.post("/api/v1/ingest", json={})).status_code == 401
            assert (await client.post(
                "/api/v1/ingest", headers={"Authorization": "Bearer test-token"},
                json={"source_id": "message", "pdf_base64": "!@#$"},
            )).status_code == 422
            assert (await client.post(
                "/api/v1/ingest", files={"file": ("bad.pdf", b"invalid", "application/pdf")},
            )).status_code == 422
            assert (await client.post(
                "/api/v1/ingest", files={"file": ("bad.pdf", b"%PDF-1.7", "application/pdf")},
                data={"region": "us"},
            )).status_code == 422
            response = await client.post("/api/v1/ingest", headers={
                "Content-Type": "multipart/form-data",
                "Content-Length": str(MAX_REQUEST_BYTES + 1),
            })
            assert response.status_code == 413

    asyncio.run(verify())


def test_worker_rejects_page_evidence_outside_the_document(store):
    pdf = (PDF_DIR / "PO-2026-420_Clean.pdf").read_bytes()
    received = asyncio.run(store.register(pdf, IntakeMetadata()))

    async def extract(_):
        return ExtractedPurchaseOrder.model_validate(example_order() | {"total_source_page": 4})

    assert asyncio.run(process_one(store, extract))
    run = asyncio.run(store.get_run(received.run_id))
    assert run is not None and run.error_code == "invalid_page_reference"
