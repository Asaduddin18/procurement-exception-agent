"""Test-only extraction worker for the two committed PDF fixtures."""

import asyncio
import os

from backend.extraction import ExtractionError
from backend.store import IngestionStore
from backend.tests.test_review import fixture_order
from backend.worker import process_one


async def extract(pages):
    for index in range(2):
        order = fixture_order(index)
        if order.po_number in pages[0]:
            return order
    raise ExtractionError("unrecognized_test_fixture")


async def main():
    store = IngestionStore(os.environ["DATABASE_URL"])
    while True:
        if not await process_one(store, extract):
            await asyncio.sleep(0.25)


if __name__ == "__main__":
    asyncio.run(main())
