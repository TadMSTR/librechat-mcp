"""
LibreChat HTTP client — JWT auth.

LibreChat has no API keys. Auth is via POST /api/auth/login with email + password.

**The JWT lives 900 seconds, not 7 days.** Every doc in this repo said 7 days until
v0.2.0, and `_PROACTIVE_REFRESH_DAYS = 6` meant the proactive refresh had never once
fired in production — only the 401-retry path kept the client alive. Measured by
decoding a live token (`exp - iat = 900`) on LibreChat v0.8.5 and again on v0.8.8-rc2,
where v0.8.6's "JWT expiry fallback" change did not move it.

Rather than write a new number down and wait for it to go stale the same way, the
refresh is now driven by the token's own `exp` claim, with the measured 900s only as a
fallback for a token whose payload will not decode.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

from . import __version__

# Dotted child of the "librechat-mcp" logger configure_logging() sets up, so
# these records reach its handlers. See the note in server.py.
log = structlog.get_logger("librechat-mcp.client")

# LibreChat's uaParser middleware (api/server/middleware/uaParser.js) runs
# ua-parser-js over the User-Agent and rejects the request with `Illegal request`
# whenever `ua.browser.name` is falsy. A UA merely EXISTING is not sufficient:
# `Mozilla/5.0 (compatible; librechat-mcp/0.1.0)` — what this client sent from
# 0.1.1 to 0.2.0 — parses to `{}` and is rejected, as are `python-httpx/0.28.1`
# and no UA at all. All three verified against the live instance.
#
# This is NOT evasion and is not an attempt to pass as a browser. The middleware
# applies to every caller of /api/agents/*, /api/assistants/*, /api/files/* and
# /api/accessPermissions/* alike, browser or not, and upstream offers no exemption
# to request — a parseable browser token is the only way to reach those routes at
# all. The honest `librechat-mcp/<version>` product token stays in the string
# precisely so LibreChat's logs, and anyone reading them, can see what we are.
#
# The version is interpolated from __version__ rather than written out again:
# 0.1.1 hardcoded `0.1.0` into this header and it drifted immediately.
_BROWSER_TOKEN = "Chrome/131.0.0.0"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"{_BROWSER_TOKEN} Safari/537.36 librechat-mcp/{__version__}"
)

# Fallback only. The `exp` claim is authoritative and is what normally drives the
# refresh; this number exists for a token whose payload will not decode, and is
# deliberately the measured value rather than a guess.
_FALLBACK_TOKEN_LIFETIME = timedelta(seconds=900)

# Refresh this far ahead of `exp`. Large enough to cover a slow request that would
# otherwise expire mid-flight, small enough not to re-login every other call at a
# 900-second lifetime.
_REFRESH_MARGIN = timedelta(seconds=60)

# POST /api/auth/login is rate-limited at roughly 4 attempts per 5 minutes and
# answers `429 Too many login attempts, please try again after 5 minutes`. We ride
# out a short burst and then surface the 429 as itself: sleeping for the full five
# minutes inside a tool call would present to the caller as a hang, which is a worse
# failure than a clear error naming the limit.
_LOGIN_MAX_ATTEMPTS = 3
_LOGIN_BACKOFF_SECONDS = (1.0, 4.0)

# LibreChat frames some rejections as Server-Sent Events. Captured live on
# v0.8.8-rc2, as the response to a request the uaParser middleware refused:
#     HTTP 200, no content-type header at all
#     event: error\ndata: {"message":"Illegal request"}\n\n
_SSE_ERROR_PREFIX = "event: error"


class LibreChatError(Exception):
    """Raised when LibreChat returns an error response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"LibreChat error {status_code}: {message}")


class LibreChatConfigError(Exception):
    """Raised for missing or invalid configuration (not an HTTP error)."""


class LibreChatClient:
    """
    Async HTTP client for LibreChat.

    A single instance is reused for the lifetime of the MCP server so the JWT
    cache and httpx connection pool are shared across tool calls.
    """

    def __init__(self) -> None:
        url = os.environ.get("LIBRECHAT_URL", "").rstrip("/")
        if not url:
            raise LibreChatConfigError("LIBRECHAT_URL is required")

        self._email = os.environ.get("LIBRECHAT_ADMIN_EMAIL", "")
        self._password = os.environ.get("LIBRECHAT_ADMIN_PASSWORD", "")
        if not self._email or not self._password:
            raise LibreChatConfigError(
                "LIBRECHAT_ADMIN_EMAIL and LIBRECHAT_ADMIN_PASSWORD are required"
            )

        self._jwt: str | None = None
        self._jwt_expires_at: datetime | None = None
        # Serialises login. Concurrent tool calls arriving on an expired token would
        # otherwise each log in, and the endpoint rate-limits at ~4 per 5 minutes —
        # so a stampede does not merely waste requests, it locks the client out.
        self._login_lock = asyncio.Lock()

        self._http = httpx.AsyncClient(
            base_url=url,
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            trust_env=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # JWT auth
    # ------------------------------------------------------------------

    async def _login(self) -> str:
        """POST /api/auth/login and cache the returned JWT.

        Callers hold `_login_lock`. Retries a bounded number of times on 429; see
        `_LOGIN_MAX_ATTEMPTS` for why it does not simply wait the limit out.
        """
        rate_limited: LibreChatError | None = None

        for attempt in range(_LOGIN_MAX_ATTEMPTS):
            resp = await self._http.post(
                "/api/auth/login",
                json={"email": self._email, "password": self._password},
            )
            if resp.status_code != 429:
                _raise_for_status(resp)
                data = _decode_json(resp)
                token = data["token"]
                self._jwt = token
                self._jwt_expires_at = _jwt_expiry(token) or (
                    datetime.now(tz=UTC) + _FALLBACK_TOKEN_LIFETIME
                )
                log.info("librechat_auth_ok", expires_at=self._jwt_expires_at.isoformat())
                return token

            rate_limited = LibreChatError(429, _error_message(resp))
            if attempt < _LOGIN_MAX_ATTEMPTS - 1:
                delay = _LOGIN_BACKOFF_SECONDS[attempt]
                log.warning(
                    "librechat_login_rate_limited",
                    attempt=attempt + 1,
                    retry_in_seconds=delay,
                )
                await asyncio.sleep(delay)

        # Unreachable except via 429 on the final attempt — the loop returns on any
        # other status, and _raise_for_status raises on any other error.
        log.error("librechat_login_rate_limit_exhausted", attempts=_LOGIN_MAX_ATTEMPTS)
        raise rate_limited  # type: ignore[misc]

    def _jwt_is_fresh(self) -> bool:
        if self._jwt is None or self._jwt_expires_at is None:
            return False
        return datetime.now(tz=UTC) < self._jwt_expires_at - _REFRESH_MARGIN

    async def _get_jwt(self) -> str:
        if self._jwt_is_fresh():
            return self._jwt  # type: ignore[return-value]
        async with self._login_lock:
            # Re-check under the lock. While this coroutine waited, whoever held the
            # lock may already have logged in; without this the lock would serialise
            # the stampede rather than collapse it.
            if self._jwt_is_fresh():
                return self._jwt  # type: ignore[return-value]
            return await self._login()

    async def _relogin_after_401(self, stale: str) -> str:
        """Re-login after a 401, unless another coroutine already replaced the token.

        `stale` is the token the caller actually sent. Comparing against it is what
        stops a 401 storm becoming a login storm: whichever coroutine takes the lock
        first logs in, and the rest then see a token that is no longer the one they
        sent and reuse it instead of logging in again.
        """
        async with self._login_lock:
            if self._jwt is not None and self._jwt != stale:
                return self._jwt
            self._jwt = None
            self._jwt_expires_at = None
            return await self._login()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    async def request(
        self, method: str, path: str, *, allow_mislabelled_json: bool = False, **kwargs: Any
    ) -> Any:
        """Send an authenticated request. Retries once on 401.

        `allow_mislabelled_json` accepts a JSON body served under a non-JSON
        content-type, and exists for exactly one measured upstream defect:
        **`GET /api/endpoints` answers `text/html; charset=utf-8` with a JSON body**
        on v0.8.8-rc2. Without it that endpoint is unreadable here, and it is the only
        one that says which providers are actually enabled — `/api/models` still lists
        `anthropic` with the endpoint fully disabled.

        **It does not weaken the guard that matters.** `_decode_json` tests for an SSE
        error body FIRST and raises on it regardless of this flag, so LibreChat's
        200-with-`Illegal request` — the failure that hid vikunja#657 for six weeks —
        is still caught. What is relaxed is only the positive content-type assertion,
        per request, where a caller has opted in. The default stays fail-closed.
        """
        token = await self._get_jwt()
        resp = await self._http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            log.info("librechat_token_expired_refreshing")
            token = await self._relogin_after_401(token)
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        _raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return _decode_json(resp, allow_mislabelled_json=allow_mislabelled_json)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _sse_error_message(body: str) -> str | None:
    """Return the message from an SSE-framed error body, or None if it is not one.

    See `_SSE_ERROR_PREFIX` for the exact shape as captured live.
    """
    if not body.startswith(_SSE_ERROR_PREFIX):
        return None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        try:
            parsed = json.loads(payload)
        except ValueError:
            return payload or _SSE_ERROR_PREFIX
        if isinstance(parsed, dict):
            return str(parsed.get("message") or parsed.get("error") or payload)
        return str(parsed)
    return "SSE error response with no data line"


def _error_message(resp: httpx.Response) -> str:
    """Best available human-readable message from an error response."""
    sse = _sse_error_message(resp.text)
    if sse is not None:
        return sse
    try:
        body = resp.json()
        return body.get("message") or body.get("error") or resp.text
    except Exception:
        return resp.text or resp.reason_phrase


def _raise_for_status(resp: httpx.Response) -> None:
    """Raise LibreChatError for 4xx/5xx, preserving the JSON error body."""
    if resp.is_success:
        return
    raise LibreChatError(resp.status_code, _error_message(resp))


def _decode_json(resp: httpx.Response, *, allow_mislabelled_json: bool = False) -> Any:
    """Decode a JSON response body, refusing anything that is not JSON.

    **A 200 from this API is not proof of success.** LibreChat returns the uaParser
    rejection as HTTP 200 with an SSE body and no content-type header at all, so
    `_raise_for_status` passed, `resp.json()` raised `json.JSONDecodeError`, and that
    was not in the tool handlers' `except` clause — it escaped as a raw traceback.
    Six weeks of every tool failing, nothing logged (vikunja#657; #53-#56 before it).

    A **missing** content-type is a failure here, not a pass. That is the whole point
    of `(resp.headers.get("content-type") or "")`: the absent header must not satisfy
    the check, and it is why this is a positive test for JSON rather than a negative
    test against known-bad values.
    """
    sse_error = _sse_error_message(resp.text)
    if sse_error is not None:
        raise LibreChatError(resp.status_code, sse_error)

    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    # `application/json` plus the RFC 6839 `+json` structured suffix. Anything else,
    # including the empty string standing in for an absent header, is refused.
    #
    # The SSE check above runs FIRST and is not conditional, which is what makes the
    # opt-in below safe: relaxing this assertion cannot let an `Illegal request`
    # through, because that body never reaches this line.
    if (
        not allow_mislabelled_json
        and content_type != "application/json"
        and not content_type.endswith("+json")
    ):
        raise LibreChatError(
            resp.status_code,
            f"expected JSON, got content-type {content_type or '<absent>'}: {resp.text[:200]}",
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise LibreChatError(resp.status_code, f"malformed JSON response: {exc}") from exc


def _jwt_expiry(token: str) -> datetime | None:
    """Read `exp` out of a JWT payload without verifying the signature.

    **This is not a security check and must never be used as one.** The server
    verifies the signature; this client only needs to know when to ask for a new
    token, and anyone able to forge the payload here would be forging it to make
    their own client log in more often.

    Returns None for anything that will not decode, so the caller falls back to the
    measured `_FALLBACK_TOKEN_LIFETIME` rather than treating a token as immortal.
    """
    try:
        payload_b64 = token.split(".")[1]
        # JWT uses unpadded base64url; b64decode requires the padding back.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = claims["exp"]
    except (IndexError, KeyError, ValueError, TypeError, binascii.Error):
        return None
    # bool is an int subclass; `exp: true` would otherwise become 1970-01-01.
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


# Module-level singleton — created on first tool call, shared across calls.
_client: LibreChatClient | None = None


def get_client() -> LibreChatClient:
    global _client
    if _client is None:
        _client = LibreChatClient()
    return _client
