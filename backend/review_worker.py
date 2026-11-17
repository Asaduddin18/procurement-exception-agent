"""Run and recover LangGraph policy jobs backed by PostgreSQL."""

import argparse
import asyncio
import logging
import os

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from backend.api import DEFAULT_DATABASE_URL
from backend.policy_evidence import EvidenceCollector
from backend.review_graph import build_graph
from backend.review_models import ReviewOutcome
from backend.review_store import ReviewJob, ReviewStore
from backend.store import MAX_JOB_ATTEMPTS
from backend.telemetry import flush, trace_stage

LOGGER = logging.getLogger(__name__)
MCP_DEFAULT_URL = "http://127.0.0.1:8001/mcp"
CHECKPOINT_SETUP_LOCK = 767319027


def interrupted(snapshot) -> bool:
    return any(task.interrupts for task in snapshot.tasks)


def projection(snapshot, job: ReviewJob) -> ReviewOutcome | None:
    timings = snapshot.values.get("node_timings", [])
    if outcome := snapshot.values.get("outcome"):
        return ReviewOutcome.model_validate(outcome | {"node_timings": timings})
    if job.kind == "EVALUATE" and interrupted(snapshot):
        return ReviewOutcome.model_validate({
            "status": "AWAITING_REVIEW", "decision": snapshot.values["decision"], "node_timings": timings,
        })
    return None


async def advance(graph, job: ReviewJob) -> ReviewOutcome:
    config = {"configurable": {"thread_id": str(job.thread_id)}}
    snapshot = await graph.aget_state(config)
    if existing := projection(snapshot, job):
        # A previous worker may have checkpointed this result before updating the run.
        return existing
    if job.attempts > MAX_JOB_ATTEMPTS:
        return ReviewOutcome(status="FAILED", resolution=job.resolution, error_code="review_attempts_exhausted")

    if job.kind == "RESUME" and interrupted(snapshot) and job.resolution is not None:
        await graph.ainvoke(Command(resume=job.resolution.model_dump(mode="json")), config, durability="sync")
    elif snapshot.next and not interrupted(snapshot):
        await graph.ainvoke(None, config, durability="sync")
    elif job.kind == "EVALUATE" and not snapshot.values:
        await graph.ainvoke({
            "run_id": str(job.run_id), "category": job.category, "region": job.region,
            "extracted_po": job.extracted_po,
        }, config, durability="sync")
    else:
        raise RuntimeError("No checkpoint can accept this policy job.")

    result = projection(await graph.aget_state(config), job)
    if result is None:
        raise RuntimeError("Policy graph did not produce a projectable state.")
    return result


async def process_review(store: ReviewStore, graph) -> bool:
    async with store.claim_review() as job:
        if job is None:
            return False
        try:
            with trace_stage(f"procurement.{job.kind.lower()}", str(job.run_id)):
                async with asyncio.timeout(90):
                    outcome = await advance(graph, job)
        except Exception as exc:
            LOGGER.warning("Review attempt failed for run %s (%s).", job.run_id, type(exc).__name__)
            await store.retry_review(job, "review_workflow_failed")
        else:
            if not await store.complete_review(job, outcome):
                LOGGER.warning("Review lease lost for run %s.", job.run_id)
        return True


async def setup_checkpointer(store: ReviewStore, saver: AsyncPostgresSaver) -> None:
    async with await store.connect() as connection:
        # PostgresSaver can build indexes concurrently. Keep the setup mutex
        # session-scoped so the setup connection is never waiting on our transaction.
        await connection.set_autocommit(True)
        await connection.execute("SELECT pg_advisory_lock(%s)", (CHECKPOINT_SETUP_LOCK,))
        await saver.setup()


async def work(database_url: str, mcp_url: str, once: bool) -> None:
    store = ReviewStore(database_url)
    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await setup_checkpointer(store, saver)
        graph = build_graph(saver, EvidenceCollector(store, mcp_url))
        while True:
            try:
                processed = await process_review(store, graph)
            except psycopg.OperationalError:
                if once:
                    raise
                LOGGER.warning("Review database unavailable. Retrying.")
                processed = False
            if once:
                return
            if not processed:
                await asyncio.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Handle at most one eligible policy job.")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    mcp_url = os.environ.get("MCP_SERVER_URL") or MCP_DEFAULT_URL
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(work(database_url, mcp_url, args.once))
    finally:
        flush()


if __name__ == "__main__":
    main()
