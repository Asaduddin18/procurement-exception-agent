import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from backend.ap_store import APStore, InvoiceLine
from backend.contracts import IntakeMetadata
from backend.store import IngestionStore
from seed import seed
from seed.fixtures import APEX

REALM = "123456"
PO_ID = "qbo-po"
RECEIVED_AT = datetime(2026, 11, 18, tzinfo=timezone.utc)


@pytest.fixture
def ap_store(database_url):
    seed.bootstrap(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute("""
            INSERT INTO qbo_purchase_orders
                (realm_id, qbo_po_id, po_number, vendor_id, qbo_vendor_id, currency, is_open, synced_at)
            VALUES (%s, %s, 'PO-AP-001', %s, 'qbo-vendor', 'USD', true, now())
        """, (REALM, PO_ID, APEX))
        # QuickBooks can return the same SKU on distinct lines with independent balances.
        connection.execute("""
            INSERT INTO qbo_purchase_order_lines
                (realm_id, qbo_po_id, qbo_line_id, qbo_item_id, sku, unit, ordered_quantity, unit_price)
            VALUES (%s, %s, '1', 'item-1', 'SS-316 Valve', 'EA', 10, 120),
                   (%s, %s, '2', 'item-1', 'SS-316 Valve', 'EA', 10, 120)
        """, (REALM, PO_ID, REALM, PO_ID))
    return APStore(database_url)


async def receipt(store, event, quantity, *, line="1"):
    return await store.record_receipt(
        realm_id=REALM, source_system="test-warehouse", receipt_event_id=event, qbo_po_id=PO_ID,
        qbo_line_id=line, quantity=Decimal(quantity), received_at=RECEIVED_AT,
    )


async def invoice(store, number, source, quantity, *, line="1", unit="EA", amount=None, linked=True):
    run = await IngestionStore(store.database_url).register(source, IntakeMetadata())
    quantity = Decimal(quantity)
    line_amount = Decimal("120.00") * quantity
    return await store.register_invoice(
        run_id=run.run_id, realm_id=REALM, vendor_id=APEX, invoice_number=number,
        currency="USD", freight_amount=Decimal(0), total_amount=line_amount if amount is None else amount,
        lines=[InvoiceLine("SS-316 Valve", unit, quantity, Decimal("120.00"), line_amount, line)],
        po_number="PO-AP-001", qbo_po_id=PO_ID if linked else None,
    )


def test_receipt_replay_and_conflicting_payload(ap_store):
    async def check():
        outcomes = await asyncio.gather(
            receipt(ap_store, "receipt-1", 4), receipt(ap_store, "receipt-1", 4),
        )
        assert sorted(outcomes) == ["duplicate", "recorded"]
        assert await receipt(ap_store, "receipt-1", 5) == "identity_conflict"
        assert await receipt(ap_store, "receipt-1", 4, line="2") == "identity_conflict"
        assert await receipt(ap_store, "receipt-2", 3) == "recorded"
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await receipt(ap_store, "receipt-3", 3, line="missing")
        with pytest.raises(psycopg.errors.CheckViolation):
            await receipt(ap_store, "receipt-4", "0.00001")

    asyncio.run(check())
    with psycopg.connect(ap_store.database_url) as connection:
        assert connection.execute(
            "SELECT count(*), sum(received_quantity) FROM goods_receipts"
        ).fetchone() == (2, Decimal(7))


def test_invoice_dedup_across_pdf_versions_and_conflicting_total(ap_store):
    async def check():
        first, reexport = await asyncio.gather(
            invoice(ap_store, "AP-123", b"first PDF", 2),
            invoice(ap_store, "  ap-123 ", b"new PDF bytes", 2),
        )
        assert sorted([first.status, reexport.status]) == ["duplicate", "registered"]
        assert first.invoice_id == reexport.invoice_id
        exact_replay = await invoice(ap_store, "AP-123", b"first PDF", 2)
        conflict = await invoice(ap_store, "AP-123", b"changed PDF", 3)
        assert exact_replay.status == "duplicate"
        assert conflict.status == "identity_conflict"
        assert conflict.invoice_id == first.invoice_id

    asyncio.run(check())
    with psycopg.connect(ap_store.database_url) as connection:
        assert connection.execute("SELECT total_amount FROM vendor_invoices").fetchall() == [(Decimal(240),)]
        assert connection.execute("SELECT count(*) FROM invoice_line_items").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM processed_events").fetchone() == (3,)


def test_reservations_serialize_and_keep_ambiguous_posts_allocated(ap_store):
    async def check():
        await receipt(ap_store, "receipt-1", 6)
        first = await invoice(ap_store, "A", b"invoice A", 4)
        second = await invoice(ap_store, "B", b"invoice B", 4)
        outcomes = await asyncio.gather(
            ap_store.reserve_invoice(first.invoice_id), ap_store.reserve_invoice(second.invoice_id),
        )
        assert sorted(outcomes) == ["insufficient_quantity", "reserved"]
        winner, loser = (first, second) if outcomes[0] == "reserved" else (second, first)
        assert await ap_store.reserve_invoice(winner.invoice_id) == "already_reserved"
        assert await ap_store.release_unsubmitted(winner.invoice_id)
        assert await ap_store.reserve_invoice(loser.invoice_id) == "reserved"
        assert sorted(await asyncio.gather(
            ap_store.begin_post(loser.invoice_id), ap_store.begin_post(loser.invoice_id),
        )) == [False, True]
        assert not await ap_store.release_unsubmitted(loser.invoice_id)
        assert await ap_store.reserve_invoice(winner.invoice_id) == "insufficient_quantity"
        assert await ap_store.record_posted(loser.invoice_id, "bill-1")
        assert await ap_store.record_posted(loser.invoice_id, "bill-1")
        assert not await ap_store.record_posted(loser.invoice_id, "other-bill")
        assert await receipt(ap_store, "receipt-2", 2) == "recorded"
        assert await ap_store.reserve_invoice(winner.invoice_id) == "reserved"

    asyncio.run(check())


def test_reservation_uses_po_line_and_rejects_missing_evidence(ap_store):
    async def check():
        await receipt(ap_store, "receipt-1", 12)
        unlinked = await invoice(ap_store, "UNLINKED", b"one", 1, line=None, linked=False)
        wrong_uom = await invoice(ap_store, "WRONG-UOM", b"two", 1, unit="BOX")
        unreceived = await invoice(ap_store, "UNRECEIVED", b"three", 1, line="2")
        over_po = await invoice(ap_store, "OVER-PO", b"four", 11)
        assert await ap_store.reserve_invoice(unlinked.invoice_id) == "not_ready"
        assert await ap_store.reserve_invoice(wrong_uom.invoice_id) == "not_ready"
        assert await ap_store.reserve_invoice(unreceived.invoice_id) == "insufficient_quantity"
        assert await ap_store.reserve_invoice(over_po.invoice_id) == "insufficient_quantity"

    asyncio.run(check())


def test_invoice_rolls_back_when_an_item_cannot_link(ap_store):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        asyncio.run(invoice(ap_store, "INVALID", b"invalid line", 1, line="missing"))
    with psycopg.connect(ap_store.database_url) as connection:
        assert connection.execute("SELECT count(*) FROM vendor_invoices").fetchone() == (0,)
