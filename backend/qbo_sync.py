"""Import QuickBooks purchase-order snapshots using exact vendor and SKU mappings."""

from datetime import datetime, timezone
from decimal import Decimal

from backend.ap_store import APStore
from backend.qbo_client import QBOClient, QBOError


async def sync_purchase_orders(store: APStore, client: QBOClient) -> int:
    started_at = datetime.now(timezone.utc)
    async with await store.connect() as connection:
        cursor = await connection.execute("""
            SELECT vendor_id, vendor_name, sku, unit, currency
            FROM vendors LEFT JOIN price_catalog USING (vendor_id)
        """)
        catalog = {}
        for row in await cursor.fetchall():
            vendor = catalog.setdefault(row["vendor_name"], (row["vendor_id"], {}))
            if row["sku"]:
                vendor[1][row["sku"], row["currency"]] = row["unit"]

    snapshots, vendor_cache, sku_cache = {}, {}, {}
    async for order in client.query("PurchaseOrder"):
        if order["POStatus"] not in ("Open", "Closed"):
            raise QBOError("Unsupported QuickBooks purchase order status.")
        qbo_vendor_id = order["VendorRef"]["value"]
        if qbo_vendor_id not in vendor_cache:
            vendor_cache[qbo_vendor_id] = (await client.read("Vendor", qbo_vendor_id))["DisplayName"]
        vendor = catalog.get(vendor_cache[qbo_vendor_id])
        if vendor is None:
            continue
        vendor_id, units = vendor
        currency = order.get("CurrencyRef", {}).get("value", "USD")
        lines = {}
        if order["POStatus"] == "Open":
            for line in order["Line"]:
                if line["DetailType"] != "ItemBasedExpenseLineDetail":
                    continue
                detail = line["ItemBasedExpenseLineDetail"]
                item_id = detail["ItemRef"]["value"]
                if item_id not in sku_cache:
                    sku_cache[item_id] = (await client.read("Item", item_id)).get("Sku")
                sku = sku_cache[item_id]
                unit = units.get((sku, currency))
                if not unit or not line.get("Id") or line["Id"] in lines:
                    raise QBOError("A QuickBooks order line has no unique ID or exact catalog mapping.")
                lines[line["Id"]] = (item_id, sku, unit, Decimal(detail["Qty"]), Decimal(detail["UnitPrice"]))
        if order["Id"] in snapshots:
            raise QBOError("QuickBooks returned a repeated purchase order. Retry the sync.")
        snapshots[order["Id"]] = (order["DocNumber"], vendor_id, qbo_vendor_id, currency,
                                  order["POStatus"] == "Open", lines)

    realm = client.realm_id
    async with await store.connect() as connection:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"qbo-po-sync:{realm}",),
        )
        cursor = await connection.execute(
            "SELECT max(synced_at) AS last_sync FROM qbo_purchase_orders WHERE realm_id = %s", (realm,),
        )
        last_sync = (await cursor.fetchone())["last_sync"]
        if last_sync is not None and last_sync > started_at:
            raise QBOError("A fresher QuickBooks PO sync has already committed.")
        for po_id, (*fields, lines) in sorted(snapshots.items()):
            await connection.execute("""
                INSERT INTO qbo_purchase_orders (
                    realm_id, qbo_po_id, po_number, vendor_id, qbo_vendor_id, currency, is_open, synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (realm_id, qbo_po_id) DO UPDATE SET
                    po_number = excluded.po_number, vendor_id = excluded.vendor_id,
                    qbo_vendor_id = excluded.qbo_vendor_id, currency = excluded.currency,
                    is_open = excluded.is_open, synced_at = excluded.synced_at
            """, (realm, po_id, *fields, started_at))
            for line_id, line in sorted(lines.items()):
                await connection.execute("""
                    INSERT INTO qbo_purchase_order_lines (
                        realm_id, qbo_po_id, qbo_line_id, qbo_item_id, sku, unit,
                        ordered_quantity, unit_price
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (realm_id, qbo_po_id, qbo_line_id) DO UPDATE SET
                        qbo_item_id = excluded.qbo_item_id, sku = excluded.sku, unit = excluded.unit,
                        ordered_quantity = excluded.ordered_quantity, unit_price = excluded.unit_price,
                        is_active = true
                """, (realm, po_id, line_id, *line))
            await connection.execute("""
                UPDATE qbo_purchase_order_lines SET is_active = false
                WHERE realm_id = %s AND qbo_po_id = %s AND NOT (qbo_line_id = ANY(%s))
            """, (realm, po_id, list(lines)))
        await connection.execute("""
            UPDATE qbo_purchase_orders SET is_open = false, synced_at = %s
            WHERE realm_id = %s AND NOT (qbo_po_id = ANY(%s))
        """, (started_at, realm, list(snapshots)))
    return sum(snapshot[4] for snapshot in snapshots.values())
