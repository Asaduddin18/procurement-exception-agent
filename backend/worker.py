"""Claim durable intake jobs and persist validated extraction outcomes."""

import argparse
import asyncio
import logging
import os

import httpx
import psycopg

from backend.api import DEFAULT_DATABASE_URL
from backend.demo_extraction import extract_fixture
from backend.extraction import Extraction, ExtractionError, build_extractor, parse_pdf
from backend.store import MAX_JOB_ATTEMPTS, IngestionStore

LOGGER = logging.getLogger(__name__)
JOB_TIMEOUT_SECONDS = 90
POLL_SECONDS = 1


async def process_one(store: IngestionStore, extract: Extraction | None) -> bool:
    job = await store.claim()
    if job is None:
        return False

    try:
        if job.attempts > MAX_JOB_ATTEMPTS:
            raise ExtractionError("job_attempts_exhausted")
        if job.pdf_bytes is None:
            raise ExtractionError("missing_pdf_input")
        async with asyncio.timeout(JOB_TIMEOUT_SECONDS):
            # Fixture mode still parses the PDF and validates the page references.
            if extract is None:
                order = extract_fixture(job.pdf_bytes)
                pages = await parse_pdf(job.pdf_bytes)
            else:
                pages = await parse_pdf(job.pdf_bytes)
                order = await extract(pages)
            if order.total_source_page > len(pages) or any(
                line.source_page > len(pages) for line in order.lines
            ):
                raise ExtractionError("invalid_page_reference")
    except TimeoutError:
        recorded = await store.finish(job, error_code="extraction_timeout")
    except ExtractionError as exc:
        recorded = await store.finish(job, error_code=str(exc))
    else:
        recorded = await store.finish(job, order=order)

    if not recorded:
        LOGGER.warning("Extraction lease lost for run %s.", job.run_id)
    return True


async def work(database_url: str, api_key: str | None, once: bool) -> None:
    store = IngestionStore(database_url)
    async with httpx.AsyncClient(timeout=20.0) as client:
        extract = build_extractor(api_key, client) if api_key else None
        while True:
            try:
                processed = await process_one(store, extract)
            except psycopg.OperationalError:
                if once:
                    raise
                LOGGER.warning("Worker database connection unavailable. Retrying.")
                processed = False
            if once:
                return
            if not processed:
                await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Handle at most one eligible job.")
    args = parser.parse_args()
    provider = os.environ.get("PROCUREMENT_EXTRACTION_PROVIDER", "deepseek")
    if provider not in {"fixture", "deepseek"}:
        parser.error("PROCUREMENT_EXTRACTION_PROVIDER must be fixture or deepseek.")
    api_key = os.environ.get("OPENAI_API_KEY", "").strip() if provider == "deepseek" else None
    if provider == "deepseek" and not api_key:
        parser.error("Set OPENAI_API_KEY to your DeepSeek API key.")
    database_url = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(work(database_url, api_key, args.once))


if __name__ == "__main__":
    main()
