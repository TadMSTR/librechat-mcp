"""Tests for librechat-mcp client and tools."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

import librechat_mcp.client as client_mod
from librechat_mcp.client import LibreChatClient, LibreChatError, _raise_for_status


# ---------------------------------------------------------------------------
# _raise_for_status
# ---------------------------------------------------------------------------

def test_raise_for_status_success():
    resp = httpx.Response(200, json={"ok": True})
    _raise_for_status(resp)  # should not raise


def test_raise_for_status_json_message():
    resp = httpx.Response(400, json={"message": "bad request"})
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(resp)
    assert "400" in str(exc_info.value)
    assert "bad request" in str(exc_info.value)


def test_raise_for_status_500():
    resp = httpx.Response(500, json={"error": "internal error"})
    with pytest.raises(LibreChatError) as exc_info:
        _raise_for_status(resp)
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# JWT auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_caches_token(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_URL", "http://librechat:3080")
    monkeypatch.setenv("LIBRECHAT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("LIBRECHAT_ADMIN_PASSWORD", "testpass")

    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": "jwt-abc"})
        )
        client = LibreChatClient()
        token = await client._login()
        assert token == "jwt-abc"
        assert client._jwt == "jwt-abc"
        assert client._jwt_acquired_at is not None
        await client.close()


@pytest.mark.asyncio
async def test_jwt_freshness(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_URL", "http://librechat:3080")
    monkeypatch.setenv("LIBRECHAT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("LIBRECHAT_ADMIN_PASSWORD", "testpass")

    with respx.mock(base_url="http://librechat:3080"):
        client = LibreChatClient()
        assert not client._jwt_is_fresh()  # no token yet

        client._jwt = "some-token"
        client._jwt_acquired_at = datetime.now(tz=timezone.utc)
        assert client._jwt_is_fresh()
        await client.close()


@pytest.mark.asyncio
async def test_request_retries_on_401(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_URL", "http://librechat:3080")
    monkeypatch.setenv("LIBRECHAT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("LIBRECHAT_ADMIN_PASSWORD", "testpass")

    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.post("/api/auth/login").mock(
            return_value=httpx.Response(200, json={"token": "fresh-token"})
        )
        mock.get("/api/agents").mock(
            side_effect=[
                httpx.Response(401, json={"message": "Unauthorized"}),
                httpx.Response(200, json=[]),
            ]
        )

        client = LibreChatClient()
        client._jwt = "stale-token"
        client._jwt_acquired_at = datetime.now(tz=timezone.utc)

        result = await client.request("GET", "/api/agents")
        assert result == []
        await client.close()


# ---------------------------------------------------------------------------
# Tool smoke tests
# ---------------------------------------------------------------------------

def _fresh_client(monkeypatch):
    """Helper: reset singleton and return a pre-authed client."""
    monkeypatch.setenv("LIBRECHAT_URL", "http://librechat:3080")
    monkeypatch.setenv("LIBRECHAT_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("LIBRECHAT_ADMIN_PASSWORD", "testpass")
    client_mod._client = None
    c = client_mod.get_client()
    c._jwt = "test-token"
    c._jwt_acquired_at = datetime.now(tz=timezone.utc)
    return c


@pytest.mark.asyncio
async def test_list_agents(monkeypatch):
    import librechat_mcp.server as srv

    c = _fresh_client(monkeypatch)
    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200, json={"agents": [{"id": "a1", "name": "Web Search"}]}
            )
        )
        result = await srv.list_agents()
        assert result["count"] == 1
        assert result["agents"][0]["name"] == "Web Search"
    await c.close()
    client_mod._client = None


@pytest.mark.asyncio
async def test_create_agent(monkeypatch):
    import librechat_mcp.server as srv

    c = _fresh_client(monkeypatch)
    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.post("/api/agents").mock(
            return_value=httpx.Response(200, json={"id": "new-id", "name": "Test Agent"})
        )
        result = await srv.create_agent(
            provider="anthropic",
            model="claude-sonnet-4-6",
            name="Test Agent",
            tools=["web_search"],
        )
        assert result["id"] == "new-id"
    await c.close()
    client_mod._client = None


@pytest.mark.asyncio
async def test_update_agent_no_fields_returns_error(monkeypatch):
    import librechat_mcp.server as srv

    _fresh_client(monkeypatch)
    result = await srv.update_agent("agent-id")
    assert "error" in result
    client_mod._client = None


@pytest.mark.asyncio
async def test_delete_agent(monkeypatch):
    import librechat_mcp.server as srv

    c = _fresh_client(monkeypatch)
    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.delete("/api/agents/a1").mock(return_value=httpx.Response(204))
        result = await srv.delete_agent("a1")
        assert result.get("deleted") is True or "agent_id" in result
    await c.close()
    client_mod._client = None


@pytest.mark.asyncio
async def test_list_tools(monkeypatch):
    import librechat_mcp.server as srv

    c = _fresh_client(monkeypatch)
    with respx.mock(base_url="http://librechat:3080") as mock:
        mock.get("/api/agents/tools").mock(
            return_value=httpx.Response(200, json=["web_search", "artifacts", "tools"])
        )
        result = await srv.list_tools()
        assert result is not None
    await c.close()
    client_mod._client = None
