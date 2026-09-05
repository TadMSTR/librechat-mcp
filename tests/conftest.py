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


@pytest.fixture
async def mcp_call(authed_client):
    """Invoke a registered tool THROUGH the FastMCP layer, as a real caller does.

    This exists because vikunja#672 was a serialisation-contract failure that no
    direct call to a tool function can ever reach. `list_tools` was annotated
    `-> dict`, returned the bare array LibreChat sends, and failed with
    `structured_content must be a dict or None` for the whole of v0.2.0 — while
    `tests/test_server.py` asserted that exact array and passed, because it called
    the undecorated function object and never crossed the boundary that breaks.

    An in-memory `Client(mcp)` runs the full path: schema validation, structured
    content, the lot. It needs no network and no live LibreChat. Returns the parsed
    structured result, and raises if the tool errored at the protocol level — so a
    tool that cannot serialise its own output fails the test rather than passing it.
    """
    from fastmcp import Client

    import librechat_mcp.server as srv

    async def _call(tool_name: str, **arguments):
        async with Client(srv.mcp) as client:
            # `raise_on_error=False` deliberately. The default raises `ToolError`,
            # which would leave the branch below unreachable and this message dead
            # code — a guard that cannot fire. Taking the result object instead lets
            # a serialisation failure be reported as itself, naming the tool.
            result = await client.call_tool(tool_name, arguments, raise_on_error=False)
            if result.is_error:
                raise AssertionError(
                    f"tool {tool_name!r} failed through the MCP layer: "
                    f"{[getattr(c, 'text', c) for c in result.content]}"
                )
            return result.data

    return _call
