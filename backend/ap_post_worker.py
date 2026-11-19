"""Recover durable QuickBooks posting jobs and publish confirmed sync results."""

import argparse
import asyncio
import logging
import os
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet

from backend.ap_store import APStore
from backend.qbo_client import QBOAuth, QBOClient, QBOConfig
from backend.qbo_posting import QBOPosting
from backend.telemetry import trace_stage

LOGGER = logging.getLogger(__name__)


async def process_post(store: APStore, posting: QBOPosting) -> bool:
    async with await store.connect() as connection:
        async with connection.transaction():
            cursor = await connection.execute("""
                SELECT j.job_id, j.run_id, j.attempts, i.invoice_id
                FROM procurement_jobs j JOIN vendor_invoices i USING (run_id)
                WHERE j.kind = 'QBO_POST' AND (
                    (j.status = 'QUEUED' AND j.available_at <= now()) OR
                    (j.status = 'RUNNING' AND j.lease_expires_at <= now())
                )
                ORDER BY j.available_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED
            """)
            job = await cursor.fetchone()
            if job is None:
                return False
            cursor = await connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked",
                (f"ap-post:{job['invoice_id']}",),
            )
            if not (await cursor.fetchone())["locked"]:
                return False
            lease = uuid4()
            await connection.execute("""
                UPDATE procurement_jobs SET status = 'RUNNING', attempts = attempts + 1,
                    lease_token = %s, lease_expires_at = now() + interval '120 seconds'
                WHERE job_id = %s
            """, (lease, job["job_id"]))
        try:
            with trace_stage("procurement.qbo_post", str(job["run_id"])):
                async with asyncio.timeout(90):
                    result = await posting.post_approved_invoice(job["invoice_id"])
        except Exception as error:
            LOGGER.warning("QuickBooks posting needs retry (%s).", type(error).__name__)
            async with connection.transaction():
                await connection.execute("""
                    UPDATE procurement_jobs SET status = %s, lease_token = NULL, lease_expires_at = NULL,
                        available_at = now() + interval '10 seconds'
                    WHERE job_id = %s AND lease_token = %s
                """, ("FAILED" if job["attempts"] >= 2 else "QUEUED", job["job_id"], lease))
                await connection.execute("""
                    UPDATE procurement_runs SET error_code = 'qbo_sync_pending' WHERE run_id = %s
                """, (job["run_id"],))
        else:
            async with connection.transaction():
                updated = await connection.execute("""
                    UPDATE procurement_jobs SET status = 'COMPLETED', lease_token = NULL, lease_expires_at = NULL
                    WHERE job_id = %s AND lease_token = %s AND lease_expires_at > now()
                """, (job["job_id"], lease))
                if updated.rowcount == 1:
                    await connection.execute("""
                        UPDATE procurement_runs SET status = 'SYNCED_TO_QBO', error_code = NULL, updated_at = now()
                        WHERE run_id = %s
                    """, (job["run_id"],))
                    await connection.execute("""
                        INSERT INTO audit_logs (run_id, event_key, event_type, details)
                        VALUES (%s, 'qbo_synced', 'SYNCED_TO_QBO',
                            jsonb_build_object('bill_id', %s::text, 'attachment_id', %s::text))
                        ON CONFLICT DO NOTHING
                    """, (job["run_id"], result.bill_id, result.attachment_id))
        return True


async def work(once=False):
    store = APStore(os.environ["DATABASE_URL"])
    config = QBOConfig(
        os.environ["QBO_REALM_ID"], os.environ["QBO_CLIENT_ID"], os.environ["QBO_CLIENT_SECRET"],
        os.environ["QBO_REDIRECT_URI"], os.environ.get("QBO_ENVIRONMENT", "sandbox"),
    )
    async with httpx.AsyncClient(timeout=15) as http:
        auth = QBOAuth(store, http, config, Fernet(os.environ["QBO_TOKEN_KEY"].encode()))
        posting = QBOPosting(store, QBOClient(http, config, auth), os.environ.get("QBO_FREIGHT_ACCOUNT_ID"))
        while True:
            processed = await process_post(store, posting)
            if once:
                return
            if not processed:
                await asyncio.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(work(args.once))


if __name__ == "__main__":
    main()
