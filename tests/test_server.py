"""Tool-level tests.

The old suite covered `get_agent` at 0%, `update_agent`'s success path at 0% and every
`_tool_error` branch at 0% — which is precisely why a tool returning a raw traceback
instead of an error dict was caught by nothing here.
"""

from __future__ import annotations

import json as _json

import httpx
import pytest
import respx

import librechat_mcp.client as client_mod
import librechat_mcp.server as srv
from librechat_mcp.client import LibreChatConfigError, LibreChatError

from .conftest import BASE_URL
from .test_client import SSE_ERROR_BODY

AGENT = {"id": "agent_abc", "name": "Web Search", "provider": "Mistral"}


# ---------------------------------------------------------------------------
# No tool may raise — the second-order defect, applied to all six at once
# ---------------------------------------------------------------------------

TOOL_CALLS = [
    pytest.param(lambda: srv.list_agents(), "GET", "/api/agents", id="list_agents"),
    pytest.param(
        lambda: srv.get_agent("agent_abc"), "GET", "/api/agents/agent_abc", id="get_agent"
    ),
    pytest.param(
        lambda: srv.create_agent(provider="Mistral", model="mistral-small-latest"),
        "POST",
        "/api/agents",
        id="create_agent",
    ),
    pytest.param(
        lambda: srv.update_agent("agent_abc", name="renamed"),
        "PATCH",
        "/api/agents/agent_abc",
        id="update_agent",
    ),
    pytest.param(
        lambda: srv.delete_agent("agent_abc"), "DELETE", "/api/agents/agent_abc", id="delete_agent"
    ),
    pytest.param(lambda: srv.list_tools(), "GET", "/api/agents/tools", id="list_tools"),
]


@pytest.mark.parametrize("call,method,path", TOOL_CALLS)
async def test_no_tool_raises_on_the_200_sse_rejection(authed_client, call, method, path):
    """The live failure, applied to every tool.

    On 2026-09-05 all six returned
    `Error calling tool '<name>': Expecting value: line 1 column 1 (char 0)` — an
    uncaught `JSONDecodeError` reaching the MCP layer. Each must now return a
    structured dict naming the real cause.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.request(method, path).mock(return_value=httpx.Response(200, content=SSE_ERROR_BODY))
        result = await call()

    assert "error" in result
    # Exact: a substring check also passes when the raw SSE body is merely echoed
    # by the content-type branch, so it cannot see the SSE parsing being removed.
    assert result["error"] == "LibreChat error 200: Illegal request"


@pytest.mark.parametrize("call,method,path", TOOL_CALLS)
async def test_no_tool_raises_on_an_unexpected_exception(authed_client, call, method, path):
    """The general form of the defect: the old clause named two exception classes, so
    any third one escaped.

    A transport error is used deliberately — it is not a `LibreChatError` and never
    passes through `_decode_json`, so nothing on the happy path can quietly convert
    it into an expected one and make this pass for the wrong reason.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.request(method, path).mock(side_effect=httpx.ConnectError("connection refused"))
        result = await call()

    assert "error" in result
    # The class is part of the diagnosis for an error nobody anticipated.
    assert "ConnectError" in result["error"]


# ---------------------------------------------------------------------------
# _tool_error branches
# ---------------------------------------------------------------------------


def test_tool_error_reports_an_expected_error_by_its_message_alone():
    assert srv._tool_error("list_agents", LibreChatError(403, "Illegal request")) == {
        "error": "LibreChat error 403: Illegal request"
    }


def test_tool_error_prefixes_an_unexpected_error_with_its_class():
    assert srv._tool_error("list_agents", ValueError("something odd")) == {
        "error": "ValueError: something odd"
    }


def test_tool_error_treats_a_config_error_as_expected():
    assert srv._tool_error("list_agents", LibreChatConfigError("LIBRECHAT_URL is required")) == {
        "error": "LIBRECHAT_URL is required"
    }


def test_tool_error_truncates_to_200_chars():
    """Retained from the F-03 fix — a tool result is model-visible and an error
    message can carry a whole response body."""
    assert len(srv._tool_error("list_agents", LibreChatError(500, "x" * 5000))["error"]) == 200


# ---------------------------------------------------------------------------
# agent_id validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    ["", "../../etc/passwd", "agent abc", "agent/abc", "agent?x=1", "agent#frag", "a.b"],
)
@pytest.mark.parametrize("tool", ["get_agent", "update_agent", "delete_agent"])
async def test_agent_id_is_validated_before_path_interpolation(authed_client, tool, bad_id):
    """Assert on the ABSENCE of traffic, not just on the error string.

    A tool that requested first and validated second would still return the error and
    still pass a check that only looked at the return value.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        catch_all = mock.route().mock(return_value=httpx.Response(200, json={}))
        result = await getattr(srv, tool)(bad_id)

    assert "Invalid agent_id" in result["error"]
    assert catch_all.call_count == 0


async def test_a_real_agent_id_is_accepted(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_PBKCguOzp3YCOYL9dumBb").mock(
            return_value=httpx.Response(200, json=AGENT)
        )
        assert await srv.get_agent("agent_PBKCguOzp3YCOYL9dumBb") == AGENT


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


async def test_list_agents_reads_the_cursor_paginated_envelope(authed_client):
    """The live shape on v0.8.8-rc2: {object, data, first_id, last_id, has_more, after}."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [AGENT],
                    "first_id": "a",
                    "last_id": "z",
                    "has_more": False,
                    "after": None,
                },
            )
        )
        result = await srv.list_agents()

    assert result["count"] == 1
    assert result["agents"][0]["name"] == "Web Search"
    assert result["has_more"] is False


async def test_list_agents_reports_truncation_instead_of_hiding_it(authed_client):
    """A caller that cannot see `has_more` cannot tell a complete list from a
    truncated one. The silence was the defect, not the single-page fetch."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200, json={"data": [AGENT], "has_more": True, "after": "cursor_xyz", "last_id": "z"}
            )
        )
        result = await srv.list_agents()

    assert result["has_more"] is True
    assert result["after"] == "cursor_xyz"


async def test_list_agents_falls_back_to_last_id_when_after_is_absent(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200, json={"data": [AGENT], "has_more": True, "last_id": "z"}
            )
        )
        assert (await srv.list_agents())["after"] == "z"


async def test_list_agents_accepts_a_bare_list_response(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(return_value=httpx.Response(200, json=[AGENT]))
        result = await srv.list_agents()
    assert result["count"] == 1
    assert result["has_more"] is False


async def test_list_agents_handles_a_non_collection_response(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(return_value=httpx.Response(200, json="unexpected"))
        assert await srv.list_agents() == {"agents": [], "count": 0, "has_more": False}


async def test_list_agents_clamps_limit_to_100(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))
        await srv.list_agents(limit=5000)
    assert route.calls.last.request.url.params["limit"] == "100"


async def test_list_agents_passes_search_through_and_omits_it_when_empty(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))
        await srv.list_agents(search="web")
        assert route.calls.last.request.url.params["search"] == "web"

        await srv.list_agents()
        assert "search" not in route.calls.last.request.url.params


# ---------------------------------------------------------------------------
# get_agent / create_agent / update_agent / delete_agent / list_tools
# ---------------------------------------------------------------------------


async def test_get_agent_returns_the_agent(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_abc").mock(return_value=httpx.Response(200, json=AGENT))
        assert await srv.get_agent("agent_abc") == AGENT


async def test_get_agent_returns_an_empty_dict_for_an_empty_body(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_abc").mock(return_value=httpx.Response(204))
        assert await srv.get_agent("agent_abc") == {}


async def test_create_agent_sends_only_the_fields_given(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(200, json={"id": "new-id"})
        )
        result = await srv.create_agent(provider="Mistral", model="mistral-small-latest")

    assert result["id"] == "new-id"
    assert _json.loads(route.calls.last.request.content) == {
        "provider": "Mistral",
        "model": "mistral-small-latest",
    }


async def test_create_agent_forwards_every_optional_field(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(200, json={"id": "new-id"})
        )
        await srv.create_agent(
            provider="Mistral",
            model="mistral-small-latest",
            name="Test Agent",
            description="desc",
            instructions="do things",
            tools=["web_search"],
            conversation_starters=["hi"],
            model_parameters={"temperature": 0.2},
        )

    assert set(_json.loads(route.calls.last.request.content)) == {
        "provider",
        "model",
        "name",
        "description",
        "instructions",
        "tools",
        "conversation_starters",
        "model_parameters",
    }


async def test_create_agent_returns_an_empty_dict_for_an_empty_body(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/agents").mock(return_value=httpx.Response(204))
        assert await srv.create_agent(provider="Mistral", model="m") == {}


async def test_update_agent_success_path(authed_client):
    """0% covered before v0.2.0."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.patch("/api/agents/agent_abc").mock(
            return_value=httpx.Response(200, json={**AGENT, "name": "renamed"})
        )
        result = await srv.update_agent("agent_abc", name="renamed")

    assert result["name"] == "renamed"
    assert _json.loads(route.calls.last.request.content) == {"name": "renamed"}


async def test_update_agent_sends_a_partial_body_only(authed_client):
    """Unset fields must be ABSENT, not sent as null — a null clears the field."""
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.patch("/api/agents/agent_abc").mock(
            return_value=httpx.Response(200, json=AGENT)
        )
        await srv.update_agent(
            "agent_abc",
            provider="Mistral",
            model="mistral-large-latest",
            description="d",
            instructions="i",
            tools=[],
            conversation_starters=[],
            model_parameters={},
        )

    body = _json.loads(route.calls.last.request.content)
    assert "name" not in body
    # Empty collections are meaningful on a PATCH — they mean "clear this" — so
    # unlike create_agent's truthiness test, update_agent must forward them.
    assert body["tools"] == []
    assert body["conversation_starters"] == []
    assert body["model_parameters"] == {}


async def test_update_agent_with_no_fields_returns_an_error(authed_client):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        catch_all = mock.route().mock(return_value=httpx.Response(200, json={}))
        result = await srv.update_agent("agent_abc")

    assert "No fields to update" in result["error"]
    assert catch_all.call_count == 0


async def test_update_agent_returns_an_empty_dict_for_an_empty_body(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.patch("/api/agents/agent_abc").mock(return_value=httpx.Response(204))
        assert await srv.update_agent("agent_abc", name="x") == {}


async def test_delete_agent_synthesises_a_result_for_a_204(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.delete("/api/agents/agent_abc").mock(return_value=httpx.Response(204))
        assert await srv.delete_agent("agent_abc") == {"deleted": True, "agent_id": "agent_abc"}


async def test_delete_agent_passes_a_body_through_when_one_is_returned(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.delete("/api/agents/agent_abc").mock(
            return_value=httpx.Response(200, json={"deleted": "agent_abc"})
        )
        assert await srv.delete_agent("agent_abc") == {"deleted": "agent_abc"}


async def test_list_tools_returns_the_capability_list(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/tools").mock(
            return_value=httpx.Response(200, json=[{"pluginKey": "web_search"}])
        )
        assert await srv.list_tools() == [{"pluginKey": "web_search"}]


async def test_list_tools_returns_an_empty_dict_for_an_empty_body(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/tools").mock(return_value=httpx.Response(204))
        assert await srv.list_tools() == {}


# ---------------------------------------------------------------------------
# Configuration errors
# ---------------------------------------------------------------------------


async def test_missing_configuration_surfaces_as_a_tool_error(monkeypatch):
    """`get_client()` raises `LibreChatConfigError`, and until v0.2.0 that call sat
    OUTSIDE the try block whose `except` clause named the class — so the guard could
    never fire. 0% covered, and unreachable."""
    monkeypatch.delenv("LIBRECHAT_URL", raising=False)
    client_mod._client = None
    try:
        result = await srv.list_agents()
    finally:
        client_mod._client = None

    assert "LIBRECHAT_URL is required" in result["error"]
