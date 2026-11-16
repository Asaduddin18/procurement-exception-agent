"""Persist review intents, claim policy jobs and project checkpoint outcomes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from backend.review_models import AuditEvent, Resolution, ReviewOutcome
from backend.store import LEASE_SECONDS, MAX_JOB_ATTEMPTS, IngestionStore


class ReviewConflict(ValueError):
    """The request conflicts with the current review."""


class ReviewForbidden(ValueError):
    """The actor lacks the authority required by the decision."""


class ReviewNotFound(ValueError):
    """The requested run does not exist."""


@dataclass(frozen=True, slots=True)
class ReviewJob:
    job_id: UUID
    run_id: UUID
    thread_id: UUID
    kind: Literal["EVALUATE", "RESUME"]
    attempts: int
    lease_token: UUID
    category: str
    region: str
    extracted_po: dict
    resolution: Resolution | None


class ReviewStore(IngestionStore):
    async def queue_resolution(self, run_id: UUID, resolution: Resolution) -> None:
        async with await self.connect() as connection:
            cursor = await connection.execute("""
                SELECT status, decision_evidence FROM procurement_runs WHERE run_id = %s FOR UPDATE
            """, (run_id,))
            run = await cursor.fetchone()
            if run is None:
                raise ReviewNotFound("Procurement run was not found.")
            cursor = await connection.execute("""
                SELECT actor_id, role, action, notes FROM procurement_resolutions WHERE run_id = %s
            """, (run_id,))
            existing = await cursor.fetchone()
            if existing is not None:
                if Resolution.model_validate(existing) == resolution:
                    return
                raise ReviewConflict("A different resolution has already been accepted.")
            if run["status"] != "AWAITING_REVIEW":
                raise ReviewConflict("Run is not awaiting review.")
            decision = ReviewOutcome.model_validate(run["decision_evidence"]).decision
            if decision is None:
                raise ReviewConflict("Review evidence is unavailable.")
            if resolution.action == "APPROVE":
                if decision.approval_blocked:
                    raise ReviewConflict("Approval requires complete compatible evidence.")
                if decision.approval_tier == "FINANCE_DIRECTOR" and resolution.role != "FINANCE_DIRECTOR":
                    raise ReviewForbidden("Finance Director approval is required.")
            await connection.execute("""
                INSERT INTO procurement_resolutions (run_id, actor_id, role, action, notes)
                VALUES (%s, %s, %s, %s, %s)
            """, (run_id, resolution.actor_id, resolution.role, resolution.action, resolution.notes))
            await connection.execute("""
                INSERT INTO procurement_jobs (job_id, run_id, kind) VALUES (%s, %s, 'RESUME')
            """, (uuid4(), run_id))
            await connection.execute("""
                INSERT INTO audit_logs (run_id, event_key, event_type, actor_id, details)
                VALUES (%s, 'resolution_queued', 'RESOLUTION_QUEUED', %s, %s)
            """, (run_id, resolution.actor_id, Jsonb({"action": resolution.action, "role": resolution.role})))

    @asynccontextmanager
    async def claim_review(self) -> AsyncIterator[ReviewJob | None]:
        async with await self.connect() as connection:
            # The session lock survives the short claim transaction and serializes checkpoint writes.
            async with connection.transaction():
                cursor = await connection.execute("""
                    SELECT j.job_id, j.run_id, j.kind, j.attempts, r.thread_id, r.category,
                           r.region, r.extracted_po, p.actor_id, p.role, p.action, p.notes
                    FROM procurement_jobs j JOIN procurement_runs r USING (run_id)
                    LEFT JOIN procurement_resolutions p USING (run_id)
                    WHERE j.kind IN ('EVALUATE', 'RESUME') AND (
                        (j.status = 'QUEUED' AND j.available_at <= clock_timestamp()) OR
                        (j.status = 'RUNNING' AND j.lease_expires_at <= clock_timestamp())
                    )
                    ORDER BY j.available_at, j.job_id
                    LIMIT 1 FOR UPDATE OF j SKIP LOCKED
                """)
                row = await cursor.fetchone()
                job = None
                if row is not None:
                    cursor = await connection.execute("""
                        SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked
                    """, (str(row["thread_id"]),))
                    if (await cursor.fetchone())["locked"]:
                        lease_token = uuid4()
                        await connection.execute("""
                            UPDATE procurement_jobs SET status = 'RUNNING', attempts = attempts + 1,
                                lease_token = %s,
                                lease_expires_at = clock_timestamp() + make_interval(secs => %s)
                            WHERE job_id = %s
                        """, (lease_token, LEASE_SECONDS, row["job_id"]))
                        fields = {key: row.pop(key) for key in ("actor_id", "role", "action", "notes")}
                        resolution = Resolution.model_validate(fields) if fields["actor_id"] else None
                        row["attempts"] += 1
                        job = ReviewJob(**row, lease_token=lease_token, resolution=resolution)
            yield job

    async def complete_review(self, job: ReviewJob, outcome: ReviewOutcome) -> bool:
        async with await self.connect() as connection:
            cursor = await connection.execute("""
                UPDATE procurement_jobs SET status = %s, lease_token = NULL, lease_expires_at = NULL
                WHERE job_id = %s AND status = 'RUNNING' AND lease_token = %s
                AND lease_expires_at > clock_timestamp() RETURNING run_id
            """, ("FAILED" if outcome.status == "FAILED" else "COMPLETED", job.job_id, job.lease_token))
            if await cursor.fetchone() is None:
                return False
            await connection.execute("""
                UPDATE procurement_runs SET status = %s, decision_evidence = %s, error_code = %s,
                    updated_at = clock_timestamp() WHERE run_id = %s
            """, (outcome.status, Jsonb(outcome.model_dump(mode="json")), outcome.error_code, job.run_id))
            event_key = "policy_finished" if job.kind == "EVALUATE" else "resolution_finished"
            event_type = {
                "AWAITING_REVIEW": "REVIEW_REQUIRED", "APPROVED": "PO_APPROVED",
                "DISPUTED": "VENDOR_DISPUTE_DRAFTED", "REJECTED": "PO_REJECTED", "FAILED": "REVIEW_FAILED",
            }[outcome.status]
            actor = outcome.resolution.actor_id if outcome.resolution else None
            await connection.execute("""
                INSERT INTO audit_logs (run_id, event_key, event_type, actor_id, details)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (run_id, event_key) DO NOTHING
            """, (job.run_id, event_key, event_type, actor, Jsonb({
                "status": outcome.status,
                "approval_tier": outcome.decision.approval_tier if outcome.decision else None,
                "finding_codes": [item.code for item in outcome.decision.findings] if outcome.decision else [],
                "error_code": outcome.error_code,
            })))
        return True

    async def retry_review(self, job: ReviewJob, error_code: str) -> None:
        if job.attempts >= MAX_JOB_ATTEMPTS:
            await self.complete_review(job, ReviewOutcome(
                status="FAILED", resolution=job.resolution, error_code=error_code,
            ))
            return
        async with await self.connect() as connection:
            await connection.execute("""
                UPDATE procurement_jobs SET status = 'QUEUED', lease_token = NULL,
                    lease_expires_at = NULL, available_at = clock_timestamp() + interval '2 seconds'
                WHERE job_id = %s AND status = 'RUNNING' AND lease_token = %s
                    AND lease_expires_at > clock_timestamp()
            """, (job.job_id, job.lease_token))

    async def get_audit(self, run_id: UUID) -> list[AuditEvent]:
        async with await self.connect() as connection:
            cursor = await connection.execute("""
                SELECT EXISTS (SELECT 1 FROM procurement_runs WHERE run_id = %s) AS present
            """, (run_id,))
            if not (await cursor.fetchone())["present"]:
                raise ReviewNotFound("Procurement run was not found.")
            cursor = await connection.execute("""
                SELECT event_type, actor_id, details, created_at FROM audit_logs
                WHERE run_id = %s ORDER BY audit_id
            """, (run_id,))
            return [AuditEvent.model_validate(row) for row in await cursor.fetchall()]
