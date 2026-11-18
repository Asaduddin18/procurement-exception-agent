import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from urllib.parse import parse_qs

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet

from backend import qbo_client
from backend.ap_store import APStore, InvoiceLine
from backend.contracts import IntakeMetadata
from backend.qbo_client import QBOAuth, QBOClient, QBOConfig, QBOError
from backend.qbo_posting import QBOPosting
from backend.qbo_sync import sync_purchase_orders
from backend.store import IngestionStore
from seed import seed

CONFIG = QBOConfig("12345", "test-app", "test-secret", "http://localhost:8000/callback")
PDF = b"%PDF-1.7\noriginal fixture bytes\n%%EOF"


class Access:
    async def access_token(self, rejected=None):
        return "rotated-access" if rejected else "test-access"


class Sandbox:
    def __init__(self):
        self.order_lines = [
            {"Id": str(i), "DetailType": "ItemBasedExpenseLineDetail", "ItemBasedExpenseLineDetail": {
                "ItemRef": {"value": "30"}, "Qty": 5, "UnitPrice": 120,
            }} for i in (1, 2)
        ]
        self.bill = self.attachment = None
        self.bill_writes = self.upload_writes = 0
        self.lose_bill_response = self.lose_upload_response = False
        self.reject_upload_once = False

    def __call__(self, request):
        path = request.url.path.removeprefix("/v3/company/12345")
        assert request.url.host == "sandbox-quickbooks.api.intuit.com"
        assert request.headers["Authorization"] == "Bearer test-access"
        if path == "/query":
            query = request.url.params["query"]
            entity = query.split()[3]
            if entity == "PurchaseOrder":
                rows = [{
                    "Id": "100", "DocNumber": "AP-DEMO", "POStatus": "Open", "VendorRef": {"value": "20"},
                    "CurrencyRef": {"value": "USD"}, "Line": self.order_lines,
                }]
            elif entity == "Bill":
                rows = [self.bill] if self.bill else []
            else:
                assert entity == "Attachable"
                rows = [self.attachment] if self.attachment else []
            return httpx.Response(200, json={"QueryResponse": {entity: rows}})
        if path == "/vendor/20":
            return httpx.Response(200, json={"Vendor": {"DisplayName": "Apex Industrial Supply"}})
        if path == "/item/30":
            return httpx.Response(200, json={"Item": {"Sku": "SS-316 Valve"}})
        if path == "/bill":
            self.bill_writes += 1
            invoice = json.loads(request.content)
            assert request.url.params["requestid"].startswith("ap-bill-")
            assert invoice["DocNumber"] == "INV-301"
            assert invoice["Line"][0]["LinkedTxn"] == [{"TxnId": "100", "TxnType": "PurchaseOrder", "TxnLineId": "1"}]
            assert invoice["LinkedTxn"] == [{"TxnId": "100", "TxnType": "PurchaseOrder"}]
            assert "Audit " in invoice["PrivateNote"] and "approved with evidence" in invoice["PrivateNote"]
            assert invoice["Line"][0]["ItemBasedExpenseLineDetail"]["ItemRef"] == {"value": "30"}
            self.bill = {"Id": "200", "TotalAmt": 120, **invoice}
            if self.lose_bill_response:
                raise httpx.ReadTimeout("lost bill response")
            return httpx.Response(200, json={"Bill": self.bill})
        if path == "/upload":
            self.upload_writes += 1
            assert request.url.params["requestid"].startswith("ap-pdf-")
            assert PDF in request.content
            assert b'name="file_metadata_01"' in request.content
            assert b'name="file_content_01"' in request.content
            assert b'"type": "Bill", "value": "200"' in request.content
            if self.reject_upload_once:
                self.reject_upload_once = False
                return httpx.Response(429, headers={"Retry-After": "60"}, json={"Fault": {}})
            file_name = request.content.split(b'filename="procurement-ap-')[1].split(b'"')[0]
            self.attachment = {
                "Id": "400", "FileName": "procurement-ap-" + file_name.decode(),
                "AttachableRef": [{"EntityRef": {"type": "Bill", "value": "200"}}],
            }
            if self.lose_upload_response:
                raise httpx.ReadTimeout("lost attachment response")
            return httpx.Response(200, json={"AttachableResponse": [{"Attachable": self.attachment}]})
        pytest.fail(f"Unexpected request to {path}")


def test_oauth_exchange_encrypts_and_serializes_refresh(database_url):
    seed.bootstrap(database_url)
    calls = []
    cipher = Fernet(Fernet.generate_key())

    def exchange(request):
        form = parse_qs(request.content.decode())
        assert request.url.host == "oauth.platform.intuit.com"
        assert request.headers["Authorization"].startswith("Basic ")
        calls.append(form)
        count = len(calls)
        return httpx.Response(200, json={
            "access_token": f"access-{count}", "refresh_token": f"refresh-{count}",
            "expires_in": 3600, "x_refresh_token_expires_in": 86400,
        })

    async def check():
        store = APStore(database_url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as http:
            auth = QBOAuth(store, http, CONFIG, cipher)
            await auth.connect("auth-code")
            assert await auth.access_token() == "access-1"
            assert await asyncio.gather(auth.access_token("access-1"), auth.access_token("access-1")) == [
                "access-2", "access-2",
            ]
            assert calls == [
                {"grant_type": ["authorization_code"], "code": ["auth-code"], "redirect_uri": [CONFIG.redirect_uri]},
                {"grant_type": ["refresh_token"], "refresh_token": ["refresh-1"]},
            ]
            async with await store.connect() as connection:
                row = await (await connection.execute("SELECT * FROM qbo_tokens")).fetchone()
                assert row["refresh_token_ciphertext"] != b"refresh-2"
                assert cipher.decrypt(row["refresh_token_ciphertext"]) == b"refresh-2"
                assert cipher.decrypt(row["access_token_ciphertext"]) == b"access-2"

    asyncio.run(check())


def test_query_pagination_and_401_recovery():
    calls = []

    def send(request):
        start = int(request.url.params["query"].split(" STARTPOSITION ")[1].split()[0])
        calls.append((start, request.headers["Authorization"]))
        if len(calls) == 1:
            return httpx.Response(401)
        ids = range(1, 101) if start == 1 else range(101, 103)
        return httpx.Response(200, json={"QueryResponse": {"Bill": [{"Id": i} for i in ids]}})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            client = QBOClient(http, CONFIG, Access())
            assert [row["Id"] async for row in client.query("Bill")] == list(range(1, 103))
        assert calls == [(1, "Bearer test-access"), (1, "Bearer rotated-access"), (101, "Bearer test-access")]

    asyncio.run(check())


def test_reads_retry_transients_and_writes_retry_explicit_throttles(monkeypatch):
    calls = []
    monkeypatch.setattr(qbo_client, "retry_delay", lambda *_: 0)

    def send(request):
        calls.append((request.method, request.url.params.get("requestid"), request.content))
        statuses = (503, 429, 200, 429, 200, 503)
        return httpx.Response(statuses[len(calls) - 1], json={})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
            client = QBOClient(http, CONFIG, Access())
            await client.request("GET", "/query")
            await client.request("POST", "/bill", params={"requestid": "one"}, content="payload")
            with pytest.raises(QBOError, match="HTTP 503"):
                await client.request("POST", "/bill", params={"requestid": "two"}, content="payload")
        assert calls[:3] == [("GET", None, b"")] * 3
        assert calls[3:5] == [("POST", "one", b"payload")] * 2
        assert calls[5:] == [("POST", "two", b"payload")]

    asyncio.run(check())


async def create_invoice(store, client, *, approve=True):
    assert await sync_purchase_orders(store, client) == 1
    run = await IngestionStore(store.database_url).register(PDF, IntakeMetadata())
    invoice = await store.register_invoice(
        run_id=run.run_id, realm_id=CONFIG.realm_id, vendor_id="apex-industrial", invoice_number="INV-301",
        currency="USD", freight_amount=Decimal(0), total_amount=Decimal(120), po_number="AP-DEMO", qbo_po_id="100",
        lines=[InvoiceLine("SS-316 Valve", "EA", Decimal(1), Decimal(120), Decimal(120), "1")],
    )
    await store.record_receipt(
        realm_id=CONFIG.realm_id, source_system="warehouse", receipt_event_id="receipt",
        qbo_po_id="100", qbo_line_id="1", quantity=Decimal(2), received_at=datetime.now(timezone.utc),
    )
    if approve:
        async with await store.connect() as connection:
            await connection.execute("UPDATE procurement_runs SET status = 'APPROVED' WHERE run_id = %s", (run.run_id,))
            await connection.execute("""
                INSERT INTO audit_logs (run_id, event_key, event_type, details)
                VALUES (%s, 'ap-approved', 'AP_INVOICE_APPROVED', '{"notes":"approved with evidence"}')
            """, (run.run_id,))
    return invoice.invoice_id


def test_sync_preserves_historical_lines_and_blocks_removed_items(database_url):
    seed.bootstrap(database_url)
    sandbox = Sandbox()

    async def check():
        store = APStore(database_url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http:
            client = QBOClient(http, CONFIG, Access())
            invoice_id = await create_invoice(store, client)
            sandbox.order_lines.pop(0)
            assert await sync_purchase_orders(store, client) == 1
            assert await store.reserve_invoice(invoice_id) == "not_ready"

    asyncio.run(check())
    with psycopg.connect(database_url) as connection:
        assert connection.execute("""
            SELECT qbo_line_id, sku, is_active FROM qbo_purchase_order_lines ORDER BY qbo_line_id
        """).fetchall() == [("1", "SS-316 Valve", False), ("2", "SS-316 Valve", True)]
        assert connection.execute("SELECT count(*) FROM goods_receipts").fetchone() == (1,)


def test_post_requires_durable_ap_approval(database_url):
    seed.bootstrap(database_url)
    sandbox = Sandbox()

    async def check():
        store = APStore(database_url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http:
            client = QBOClient(http, CONFIG, Access())
            invoice_id = await create_invoice(store, client, approve=False)
            assert await store.reserve_invoice(invoice_id) == "reserved"
            with pytest.raises(QBOError, match="approved invoice"):
                await QBOPosting(store, client).post_approved_invoice(invoice_id)
            assert sandbox.bill_writes == 0

    asyncio.run(check())


@pytest.mark.parametrize("lost_bill,lost_pdf,rejected_pdf", [
    (False, False, False),
    (True, True, False),
    (False, False, True),
])
def test_bill_and_pdf_reconcile_without_recreating_bill(database_url, lost_bill, lost_pdf, rejected_pdf):
    seed.bootstrap(database_url)
    sandbox = Sandbox()
    sandbox.lose_bill_response = lost_bill
    sandbox.lose_upload_response = lost_pdf
    sandbox.reject_upload_once = rejected_pdf

    async def check():
        store = APStore(database_url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sandbox)) as http:
            client = QBOClient(http, CONFIG, Access())
            invoice_id = await create_invoice(store, client)
            assert await store.reserve_invoice(invoice_id) == "reserved"
            posting = QBOPosting(store, client)
            if lost_bill:
                with pytest.raises(QBOError, match="did not return"):
                    await posting.post_approved_invoice(invoice_id)
                assert not await store.release_unsubmitted(invoice_id)
            if lost_pdf or rejected_pdf:
                with pytest.raises(QBOError):
                    await posting.post_approved_invoice(invoice_id)
            result = await posting.post_approved_invoice(invoice_id)
            assert (result.bill_id, result.attachment_id) == ("200", "400")
            assert await posting.post_approved_invoice(invoice_id) == result
            assert sandbox.bill_writes == 1
            assert sandbox.upload_writes == (2 if rejected_pdf else 1)

    asyncio.run(check())
