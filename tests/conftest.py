"""Shared fixtures.

Deliberately two narrow fixtures rather than one that does both: a test that only
needs the environment should not be made to construct an HTTP client, and a fixture
serving two purposes is one whose next edit breaks a dozen unrelated tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import librechat_mcp.client as client_mod

BASE_URL = "http://librechat:3080"


@pytest.fixture
def librechat_env(monkeypatch):
    """The three env vars `LibreChatClient.__init__` requires. Nothing else."""
    monkeypatch.setenv("LIBRECHAT_URL", BASE_URL)
    monkeypatch.setenv("LIBRECHAT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("LIBRECHAT_ADMIN_PASSWORD", "testpass")


@pytest.fixture
async def authed_client(librechat_env):
    """The module singleton, holding a token far enough from expiry not to refresh.

    Resets `_client` both before and after. It is module-global state: a test that
    left one behind would hand the next test a client built against another test's
    environment, and a test that found one already there would silently exercise it.
    """
    client_mod._client = None
    client = client_mod.get_client()
    client._jwt = "test-token"
    client._jwt_expires_at = datetime.now(tz=UTC) + timedelta(hours=1)
    yield client
    await client.close()
    client_mod._client = None
