"""Talks to the Urja portal so that nothing else in the app has to.

Everything annoying lives here: login, the session dying, the signed export
call, the rate limit. The rest of the code just asks for data.
"""

import asyncio
import hashlib
import hmac
import logging
import random
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class PortalError(Exception):
    """Portal is down, or kept failing after we retried."""


class PortalAuthError(PortalError):
    """Login itself failed. Retrying won't help, the config is probably wrong."""


class PortalNotFound(PortalError):
    pass


def sign_request(secret: str, method: str, path: str, query: str, timestamp: str) -> str:
    # copied from what the Transformers page does in the browser when you
    # click "Export all meters". Fields joined by newlines, then HMAC-SHA256.
    message = "\n".join([method, path, query, timestamp])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class PortalClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        email: str,
        password: str,
        min_gap_seconds: float = 0.7,
        max_attempts: int = 5,
        backoff_base_seconds: float = 2.0,
    ):
        self._http = http
        self._email = email
        self._password = password
        self._min_gap = min_gap_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds

        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self._signing_secret: str | None = None

        # one call at a time is slow, but the limit seems to be per session
        # and being polite here is way simpler than recovering from 429s
        self._pace_lock = asyncio.Lock()
        self._last_call = 0.0

    async def login(self, force: bool = False) -> None:
        async with self._login_lock:
            # someone else may have logged in while we waited for the lock
            if self._logged_in and not force:
                return

            self._http.cookies.clear()
            self._logged_in = False
            # it's a normal SvelteKit form action. The Origin header matters,
            # SvelteKit rejects cross-site form posts without it.
            try:
                resp = await self._http.post(
                    "/login",
                    data={"email": self._email, "password": self._password},
                    headers={"Origin": str(self._http.base_url).rstrip("/")},
                    follow_redirects=False,
                )
                # a bad password can also come back as a 200 page with an error
                # on it, so the status alone proves nothing. Ask for something
                # that needs a session instead.
                check = await self._http.get("/portal/dts", params={"page": 1})
            except httpx.TransportError as exc:
                raise PortalError(f"couldn't reach the portal: {exc}") from exc
            # a 429 or 500 here means the portal is struggling, not that the
            # password is wrong. Mixing those up cost me a failing test.
            if check.status_code == 429 or check.status_code >= 500:
                raise PortalError(f"portal unhealthy during login: HTTP {check.status_code}")
            if check.status_code != 200:
                raise PortalAuthError(
                    f"login failed (POST /login gave {resp.status_code}, "
                    f"session check gave {check.status_code})"
                )
            self._logged_in = True
            log.info("logged in to portal")

    async def _wait_for_turn(self) -> None:
        async with self._pace_lock:
            wait = self._last_call + self._min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    async def _backoff(self, attempt: int) -> None:
        # 429s come with no Retry-After header so we have to guess
        delay = self._backoff_base * (2**attempt) + random.uniform(0, 0.5)
        log.warning("portal busy, sleeping %.1fs", delay)
        await asyncio.sleep(delay)

    async def _get_json(
        self, path: str, params: dict | None = None, signed: bool = False
    ) -> dict[str, Any]:
        if not self._logged_in:
            await self.login()

        relogged = False
        resigned = False
        last_problem = "unknown"

        for attempt in range(self._max_attempts):
            await self._wait_for_turn()
            headers = await self._signature_headers(path, params) if signed else {}
            try:
                resp = await self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_problem = f"network: {exc}"
                await self._backoff(attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    # e.g. an HTML page, a proxy error page etc.
                    last_problem = "200 but not JSON"
                    await self._backoff(attempt)
                    continue

            error = _error_code(resp)

            if resp.status_code == 404 and "application/json" in resp.headers.get(
                "content-type", ""
            ):
                raise PortalNotFound(path)

            if resp.status_code == 401 and error == "signature_invalid" and not resigned:
                # secret may have rotated, grab it again and retry once
                self._signing_secret = None
                resigned = True
                continue

            if resp.status_code == 401 and error != "signature_invalid" and not relogged:
                # session died. Log in again and carry on, callers never see this
                log.info("portal session expired, logging in again")
                self._signing_secret = None
                relogged = True
                await self.login(force=True)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_problem = f"HTTP {resp.status_code}"
                await self._backoff(attempt)
                continue

            raise PortalError(f"GET {path} gave HTTP {resp.status_code} ({error})")

        raise PortalError(f"gave up on GET {path}: {last_problem}")

    async def _signature_headers(self, path: str, params: dict | None) -> dict[str, str]:
        if self._signing_secret is None:
            # the portal hands the secret to any logged in user. Yes, really.
            body = await self._get_json("/portal/keys")
            self._signing_secret = body["data"]["signingSecret"]
            await self._wait_for_turn()
        query = str(httpx.QueryParams(params or {}))
        timestamp = str(int(time.time()))
        return {
            "x-timestamp": timestamp,
            "x-signature": sign_request(self._signing_secret, "GET", path, query, timestamp),
        }

    async def export_meters(self) -> list[dict]:
        """Every meter in one call. This is the bulk path, see PROTOCOL.md."""
        body = await self._get_json("/portal/export", params={"page": 1}, signed=True)
        meters = body["data"]
        # right now page is ignored and we get everything. If that ever changes
        # I'd rather fail loudly than quietly serve half the fleet.
        if len(meters) != body.get("total", len(meters)):
            raise PortalError(f"export gave {len(meters)} of {body['total']} meters")
        return meters

    async def list_transformers(self) -> list[dict]:
        transformers: list[dict] = []
        page = 1
        while True:
            body = await self._get_json("/portal/dts", params={"page": page})
            transformers.extend(body["data"])
            if not body["data"] or len(transformers) >= body["total"]:
                return transformers
            page += 1

    async def readings(self, meter_id: str, start: str | None, end: str | None) -> list[dict]:
        params = {"from": start, "to": end} if start and end else None
        body = await self._get_json(f"/portal/meters/{meter_id}/energy", params=params)
        return body["data"]


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return resp.json().get("error")
    except (ValueError, AttributeError):
        return None
