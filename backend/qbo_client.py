"""QuickBooks Accounting API transport and encrypted OAuth credential lifecycle."""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

from backend.ap_store import APStore

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
HOSTS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
SCOPE = "com.intuit.quickbooks.accounting"


class QBOError(RuntimeError):
    """A sanitized connector error suitable for an operator-facing status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class QBOReconnect(QBOError):
    pass


@dataclass(frozen=True, slots=True)
class QBOConfig:
    realm_id: str
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    environment: str = "sandbox"

    def __post_init__(self):
        if not re.fullmatch(r"[0-9]+", self.realm_id) or self.environment not in HOSTS:
            raise ValueError("Configure a numeric QuickBooks realm and a valid environment.")

    def authorization_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "scope": SCOPE,
            "redirect_uri": self.redirect_uri, "state": state,
        })


class QBOAuth:
    def __init__(self, store: APStore, http: httpx.AsyncClient, config: QBOConfig, cipher: Fernet):
        self.store, self.http, self.config, self.cipher = store, http, config, cipher

    async def _exchange(self, form: dict):
        started_at = datetime.now(timezone.utc)
        try:
            response = await self.http.post(
                TOKEN_URL, data=form, auth=(self.config.client_id, self.config.client_secret),
                headers={"Accept": "application/json"}, follow_redirects=False,
            )
        except httpx.RequestError:
            raise QBOError("QuickBooks token exchange transport failed.") from None
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if not response.is_success:
                if data.get("error") == "invalid_grant":
                    raise QBOReconnect("QuickBooks authorization expired or was revoked. Reconnect the realm.")
                raise QBOError(f"QuickBooks token exchange returned HTTP {response.status_code}.", response.status_code)
            access, refresh = data["access_token"], data["refresh_token"]
            if not all(isinstance(token, str) and token for token in (access, refresh)):
                raise ValueError
            access_seconds = int(data["expires_in"])
            refresh_seconds = int(data["x_refresh_token_expires_in"])
            if min(access_seconds, refresh_seconds) <= 0:
                raise ValueError
            return (
                access, refresh, started_at + timedelta(seconds=access_seconds),
                started_at + timedelta(seconds=refresh_seconds),
            )
        except (KeyError, ValueError, TypeError, OverflowError):
            raise QBOError(f"Invalid QuickBooks token response (HTTP {response.status_code}).") from None

    async def _lock_realm(self, connection):
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"qbo-oauth:{self.config.realm_id}",),
        )

    async def _save(self, connection, tokens):
        access, refresh, access_expires_at, refresh_expires_at = tokens
        await connection.execute("""
            INSERT INTO qbo_tokens (realm_id, access_token_ciphertext, refresh_token_ciphertext,
                                    access_expires_at, refresh_expires_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (realm_id) DO UPDATE SET
                access_token_ciphertext = excluded.access_token_ciphertext,
                refresh_token_ciphertext = excluded.refresh_token_ciphertext,
                access_expires_at = excluded.access_expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                updated_at = now()
        """, (self.config.realm_id, self.cipher.encrypt(access.encode()), self.cipher.encrypt(refresh.encode()),
              access_expires_at, refresh_expires_at))

    async def connect(self, code: str) -> None:
        async with await self.store.connect() as connection:
            await self._lock_realm(connection)
            tokens = await self._exchange({
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_uri,
            })
            await self._save(connection, tokens)

    async def access_token(self, rejected: str | None = None) -> str:
        async with await self.store.connect() as connection:
            await self._lock_realm(connection)
            cursor = await connection.execute("SELECT * FROM qbo_tokens WHERE realm_id = %s", (self.config.realm_id,))
            saved = await cursor.fetchone()
            if saved is None:
                raise QBOReconnect("Connect this QuickBooks realm before making API calls.")

            now = datetime.now(timezone.utc)
            access = self.cipher.decrypt(saved["access_token_ciphertext"]).decode()
            if access != rejected and saved["access_expires_at"] > now + timedelta(seconds=30):
                return access
            if saved["refresh_expires_at"] <= now:
                raise QBOReconnect("QuickBooks refresh token expired. Reconnect the realm.")

            tokens = await self._exchange({
                "grant_type": "refresh_token",
                "refresh_token": self.cipher.decrypt(saved["refresh_token_ciphertext"]).decode(),
            })
            await self._save(connection, tokens)
            return tokens[0]


def qbo_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def retry_delay(response: httpx.Response | None, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            seconds = float(retry_after) if retry_after.isdecimal() else (
                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
            ).total_seconds()
            return max(2 ** attempt, seconds)
        except (TypeError, ValueError, OverflowError):
            pass
    return 2 ** attempt


class QBOClient:
    def __init__(self, http: httpx.AsyncClient, config: QBOConfig, auth: QBOAuth):
        self.http, self.realm_id, self.auth = http, config.realm_id, auth
        self.base = f"{HOSTS[config.environment]}/v3/company/{config.realm_id}"

    async def request(self, method: str, endpoint: str, **kwargs) -> dict:
        token = await self.auth.access_token()
        refreshed = False
        retries = 0
        extra_headers = kwargs.pop("headers", {})
        while True:
            try:
                response = await self.http.request(
                    method, self.base + endpoint, headers={
                        "Authorization": f"Bearer {token}", "Accept": "application/json",
                        **extra_headers,
                    }, follow_redirects=False, **kwargs,
                )
            except httpx.RequestError:
                if method != "GET" or retries == 2:
                    raise QBOError("QuickBooks connection did not return a response.") from None
                await asyncio.sleep(retry_delay(None, retries))
                retries += 1
                continue
            if response.status_code == 401 and not refreshed:
                token = await self.auth.access_token(rejected=token)
                refreshed = True
                continue
            retry_read = method == "GET" and response.status_code >= 500
            retry_throttle = response.status_code == 429 and (
                method == "GET" or bool(kwargs.get("params", {}).get("requestid"))
            )
            if (retry_read or retry_throttle) and retries < 2:
                delay = retry_delay(response, retries)
                if delay <= 30:
                    await asyncio.sleep(delay)
                    retries += 1
                    continue
            if not response.is_success:
                raise QBOError(f"QuickBooks API returned HTTP {response.status_code}.", response.status_code)
            try:
                data = json.loads(response.content, parse_float=Decimal)
            except ValueError:
                raise QBOError("QuickBooks API returned invalid JSON.") from None
            if not isinstance(data, dict) or "Fault" in data:
                raise QBOError("QuickBooks API returned an unexpected result.")
            return data

    async def read(self, entity: str, identifier: str) -> dict:
        if not re.fullmatch(r"[0-9]+", identifier):
            raise QBOError("Invalid QuickBooks entity ID.")
        return (await self.request("GET", f"/{entity.lower()}/{identifier}"))[entity]

    async def query(self, entity: str, where: str = ""):
        page_size = 100
        start = 1
        while True:
            query = f"SELECT * FROM {entity}{where} STARTPOSITION {start} MAXRESULTS {page_size}"
            result = (await self.request("GET", "/query", params={"query": query})).get("QueryResponse")
            if not isinstance(result, dict):
                raise QBOError("QuickBooks returned an invalid query envelope.")
            page = result.get(entity, [])
            if not isinstance(page, list) or len(page) > page_size:
                raise QBOError("QuickBooks returned an invalid query page.")
            for row in page:
                yield row
            if len(page) < page_size:
                break
            start += len(page)
