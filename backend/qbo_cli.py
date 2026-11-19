"""Operator entry points for QuickBooks authorization, PO sync, and approved bill posting."""

import argparse
import asyncio
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from secrets import compare_digest, token_urlsafe
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken

from backend.ap_store import APStore
from backend.qbo_client import QBOAuth, QBOClient, QBOConfig, QBOError
from backend.qbo_posting import QBOPosting
from backend.qbo_sync import sync_purchase_orders


def wait_for_authorization(config: QBOConfig) -> str:
    callback = urlsplit(config.redirect_uri)
    if (callback.scheme != "http" or callback.hostname not in ("localhost", "127.0.0.1")
            or callback.username or callback.password or not callback.path or callback.query or callback.fragment):
        raise QBOError("The operator OAuth callback requires a registered HTTP loopback URI.")

    state = token_urlsafe(32)
    result = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # The authorization code is part of the URL.

        def do_GET(self):
            route = urlsplit(self.path)
            params = parse_qs(route.query, keep_blank_values=True)
            supplied_state = params.get("state", [])
            codes = params.get("code", [])
            valid = (
                route.path == callback.path and len(supplied_state) == 1
                and compare_digest(supplied_state[0].encode(), state.encode())
                and params.get("realmId") == [config.realm_id]
                and len(codes) == 1 and bool(codes[0]) and "error" not in params
            )
            if valid:
                result.append(codes[0])
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Return to the terminal." if valid else b"Invalid QuickBooks authorization callback.")

    with HTTPServer(("127.0.0.1", callback.port or 80), CallbackHandler) as server:
        server.timeout = 1
        print(config.authorization_url(state), flush=True)
        deadline = monotonic() + 300
        while not result and monotonic() < deadline:
            server.handle_request()
    if not result:
        raise QBOError("No matching QuickBooks authorization callback was received.")
    return result[0]


async def run(args):
    config = QBOConfig(
        realm_id=os.environ["QBO_REALM_ID"], client_id=os.environ["QBO_CLIENT_ID"],
        client_secret=os.environ["QBO_CLIENT_SECRET"], redirect_uri=os.environ["QBO_REDIRECT_URI"],
        environment=os.environ.get("QBO_ENVIRONMENT", "sandbox"),
    )
    store = APStore(os.environ["DATABASE_URL"])
    cipher = Fernet(os.environ["QBO_TOKEN_KEY"].encode())
    async with httpx.AsyncClient(timeout=15) as http:
        auth = QBOAuth(store, http, config, cipher)
        if args.operation == "connect":
            print("Open the authorization URL in your browser and select the configured realm.")
            await auth.connect(await asyncio.to_thread(wait_for_authorization, config))
            print("QuickBooks authorization saved.")
            return
        client = QBOClient(http, config, auth)
        if args.operation == "sync":
            print(f"Synced {await sync_purchase_orders(store, client)} open purchase orders.")
            return
        posting = QBOPosting(store, client, os.environ.get("QBO_FREIGHT_ACCOUNT_ID"))
        posted = await posting.post_approved_invoice(args.invoice_id)
        print(f"Verified QuickBooks bill {posted.bill_id}, PDF attachment {posted.attachment_id}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("connect")
    subparsers.add_parser("sync")
    post = subparsers.add_parser("post")
    post.add_argument("invoice_id", type=UUID)
    try:
        asyncio.run(run(parser.parse_args()))
    except KeyError as exc:
        parser.error(f"Missing configuration or QuickBooks field {exc}.")
    except (QBOError, ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    except InvalidToken:
        parser.exit(1, "The configured key cannot decrypt the stored QuickBooks credentials.\n")


if __name__ == "__main__":
    main()
