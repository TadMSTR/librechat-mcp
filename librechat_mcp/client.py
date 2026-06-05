"""
LibreChat HTTP client — JWT auth.

LibreChat has no API keys. Auth is via POST /api/auth/login with email + password.
JWT lifetime is 7 days (LibreChat default). The token is cached in-process and refreshed
proactively after 6 days, or immediately on a 401 response.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import structlog

log = structlog.get_logger(__name__)

_PROACTIVE_REFRESH_DAYS = 6


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

        self._jwt: Optional[str] = None
        self._jwt_acquired_at: Optional[datetime] = None

        self._http = httpx.AsyncClient(
            base_url=url,
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; librechat-mcp/0.1.0)",
            },
            trust_env=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # JWT auth
    # ------------------------------------------------------------------

    async def _login(self) -> str:
        """POST /api/auth/login and cache the returned JWT."""
        resp = await self._http.post(
            "/api/auth/login",
            json={"email": self._email, "password": self._password},
        )
        _raise_for_status(resp)
        data = resp.json()
        self._jwt = data["token"]
        self._jwt_acquired_at = datetime.now(tz=timezone.utc)
        log.info("librechat_auth_ok")
        return self._jwt  # type: ignore[return-value]

    def _jwt_is_fresh(self) -> bool:
        if self._jwt is None or self._jwt_acquired_at is None:
            return False
        age = datetime.now(tz=timezone.utc) - self._jwt_acquired_at
        return age < timedelta(days=_PROACTIVE_REFRESH_DAYS)

    async def _get_jwt(self) -> str:
        if not self._jwt_is_fresh():
            return await self._login()
        return self._jwt  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an authenticated request. Retries once on 401."""
        token = await self._get_jwt()
        resp = await self._http.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        if resp.status_code == 401:
            log.info("librechat_token_expired_refreshing")
            self._jwt = None
            token = await self._login()
            resp = await self._http.request(
                method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        _raise_for_status(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _raise_for_status(resp: httpx.Response) -> None:
    """Raise LibreChatError for 4xx/5xx, preserving the JSON error body."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        msg = body.get("message") or body.get("error") or resp.text
    except Exception:
        msg = resp.text or resp.reason_phrase
    raise LibreChatError(resp.status_code, msg)


# Module-level singleton — created on first tool call, shared across calls.
_client: Optional[LibreChatClient] = None


def get_client() -> LibreChatClient:
    global _client
    if _client is None:
        _client = LibreChatClient()
    return _client
