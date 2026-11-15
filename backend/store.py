"""PostgreSQL transaction boundaries for input registration and job processing."""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.contracts import AcceptedRun, ExtractedPurchaseOrder, IntakeMetadata, RunStatus

MAX_JOB_ATTEMPTS = 3
LEASE_SECONDS = 120

CLAIM_SQL = """
WITH candidate AS (
    SELECT job_id FROM procurement_jobs
    WHERE kind = 'PROCESS' AND (
        (status = 'QUEUED' AND available_at <= clock_timestamp())
        OR (status = 'RUNNING' AND lease_expires_at <= clock_timestamp())
    )
    ORDER BY available_at, job_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE procurement_jobs AS job
SET status = 'RUNNING', attempts = job.attempts + 1, lease_token = %s,
    lease_expires_at = clock_timestamp() + make_interval(secs => %s)
FROM candidate
WHERE job.job_id = candidate.job_id
RETURNING job.job_id, job.run_id, job.attempts, job.lease_token
"""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    job_id: UUID
    run_id: UUID
    attempts: int
    lease_token: UUID
    pdf_bytes: bytes | None


class IngestionStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    async def connect(self):
        return await psycopg.AsyncConnection.connect(
            self.database_url, row_factory=dict_row,
            connect_timeout=3, options="-c statement_timeout=5000",
        )

    async def register(self, pdf_bytes: bytes, metadata: IntakeMetadata) -> AcceptedRun:
        digest = sha256(pdf_bytes).hexdigest()
        run_id, thread_id = uuid4(), uuid4()
        async with await self.connect() as connection:
            await connection.set_isolation_level(psycopg.IsolationLevel.READ_COMMITTED)
            await connection.execute("""
                INSERT INTO procurement_runs (run_id, thread_id, category, region)
                VALUES (%s, %s, %s, %s)
            """, (run_id, thread_id, metadata.category, metadata.region))
            inserted = await connection.execute("""
                INSERT INTO processed_events (sha256, run_id, source_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (sha256) DO NOTHING
                RETURNING run_id
            """, (digest, run_id, metadata.source_id))
            if await inserted.fetchone() is None:
                # This provisional run has no dependent records and was never committed.
                await connection.execute("DELETE FROM procurement_runs WHERE run_id = %s", (run_id,))
                original = await connection.execute("""
                    SELECT run_id, thread_id
                    FROM processed_events JOIN procurement_runs USING (run_id)
                    WHERE sha256 = %s
                """, (digest,))
                row = await original.fetchone()
                if row is None:
                    raise RuntimeError("Committed duplicate could not be resolved.")
                return AcceptedRun(status="duplicate_ignored", **row)

            await connection.execute(
                "INSERT INTO procurement_inputs (run_id, pdf_bytes) VALUES (%s, %s)",
                (run_id, pdf_bytes),
            )
            await connection.execute(
                "INSERT INTO procurement_jobs (job_id, run_id, kind) VALUES (%s, %s, 'PROCESS')",
                (uuid4(), run_id),
            )
            await connection.execute("""
                INSERT INTO audit_logs (run_id, event_key, event_type)
                VALUES (%s, 'intake_queued', 'INTAKE_QUEUED')
            """, (run_id,))
        return AcceptedRun(status="queued", run_id=run_id, thread_id=thread_id)

    async def get_run(self, run_id: UUID) -> RunStatus | None:
        async with await self.connect() as connection:
            cursor = await connection.execute("""
                SELECT run_id, thread_id, status, category, region, error_code, extracted_po
                FROM procurement_runs WHERE run_id = %s
            """, (run_id,))
            row = await cursor.fetchone()
        return RunStatus.model_validate(row) if row is not None else None

    async def claim(self) -> ClaimedJob | None:
        async with await self.connect() as connection:
            cursor = await connection.execute(CLAIM_SQL, (uuid4(), LEASE_SECONDS))
            job = await cursor.fetchone()
            if job is None:
                return None
            await connection.execute("""
                UPDATE procurement_runs SET status = 'PROCESSING', updated_at = clock_timestamp()
                WHERE run_id = %s
            """, (job["run_id"],))
            payload = await connection.execute(
                "SELECT pdf_bytes FROM procurement_inputs WHERE run_id = %s", (job["run_id"],)
            )
            row = await payload.fetchone()
            return ClaimedJob(**job, pdf_bytes=row["pdf_bytes"] if row else None)

    async def finish(
        self, job: ClaimedJob, *, order: ExtractedPurchaseOrder | None = None,
        error_code: str | None = None,
    ) -> bool:
        if (order is None) == (error_code is None):
            raise ValueError("Supply either a validated order or an error code.")
        async with await self.connect() as connection:
            updated = await connection.execute("""
                UPDATE procurement_jobs
                SET status = %s, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = %s AND status = 'RUNNING' AND lease_token = %s
                    AND lease_expires_at > clock_timestamp()
                RETURNING run_id
            """, ("COMPLETED" if order else "FAILED", job.job_id, job.lease_token))
            row = await updated.fetchone()
            if row is None:
                return False

            run_id = row["run_id"]
            await connection.execute("""
                UPDATE procurement_runs
                SET status = %s, po_number = %s, currency = %s, grand_total = %s,
                    extracted_po = %s, error_code = %s, updated_at = clock_timestamp()
                WHERE run_id = %s
            """, (
                "EXTRACTED" if order else "FAILED",
                order.po_number if order else None,
                order.currency if order else None,
                order.grand_total if order else None,
                Jsonb(order.model_dump(mode="json")) if order else None,
                error_code, run_id,
            ))
            await connection.execute("""
                INSERT INTO audit_logs (run_id, event_key, event_type, details)
                VALUES (%s, 'extraction_finished', %s, %s)
            """, (
                run_id, "PO_EXTRACTED" if order else "EXTRACTION_FAILED",
                Jsonb({"error_code": error_code} if error_code else {}),
            ))
        return True
