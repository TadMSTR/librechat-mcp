"""The gate this repo earned: invoke EVERY registered tool through the MCP layer.

vikunja#672 was a serialisation-contract failure. `list_tools` was annotated
`-> dict` and returned the bare array LibreChat sends, so FastMCP refused it with
`structured_content must be a dict or None` — on a *successful* upstream response.
The tool was 100% dead for the whole of v0.2.0 and its unit test was green the entire
time, because it called the undecorated function object and never crossed the
boundary that fails.

This programme has now produced a green Docker publish for an image tag that 404s, a
`200` from a zero-credit API key, a coverage gate over a tool nothing could call, and
a health assertion that could never pass. Every one was a success-shaped signal
measured at the wrong layer. This module measures at the layer the caller uses, which
is the only layer that settles the question.

Two properties are asserted, and the second is what keeps the first honest:

1. Against an upstream returning the shapes that break serialisation — a bare array,
   a bare scalar — every registered tool still returns a dict.
2. The set of tools exercised is exactly the set registered. A tool added without an
   entry here FAILS this module rather than being silently skipped, which is the
   difference between a gate and a decoration.

It needs no live LibreChat and no network.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastmcp import Client

import librechat_mcp.server as srv

from .conftest import BASE_URL

pytestmark = pytest.mark.anyio


# Minimal valid arguments per tool. A tool whose required args are absent here would
# fail on validation rather than on serialisation, which would look like coverage
# while testing nothing — so these are chosen to reach each tool's request path.
# `agent_id` values must satisfy `_AGENT_ID_RE`.
TOOL_ARGUMENTS: dict[str, dict] = {
    "list_agents": {},
    "get_agent": {"agent_id": "agent_test"},
    "create_agent": {"provider": "Mistral", "model": "mistral-small-latest"},
    "update_agent": {"agent_id": "agent_test", "name": "x"},
    "delete_agent": {"agent_id": "agent_test"},
    "list_tools": {},
}

# The shapes that break a `-> dict` tool at FastMCP's boundary. The array is the one
# observed in production (#672); the others are the same defect class and are here so
# an upstream change cannot reintroduce it through a door nobody watched.
BREAKING_UPSTREAM_BODIES = [
    pytest.param([{"pluginKey": "web_search"}], id="bare-array"),
    pytest.param([], id="empty-array"),
    pytest.param("a bare string", id="bare-string"),
    pytest.param(7, id="bare-number"),
]


async def _registered_tool_names() -> set[str]:
    async with Client(srv.mcp) as client:
        return {t.name for t in await client.list_tools()}


async def test_every_registered_tool_has_a_case_here():
    """A new tool must be added to `TOOL_ARGUMENTS` or this gate does not cover it.

    Without this, the parametrised test below would keep passing while silently
    exercising a shrinking fraction of the surface — the failure mode that let a
    coverage gate sit over a tool nothing could call.
    """
    registered = await _registered_tool_names()
    missing = registered - TOOL_ARGUMENTS.keys()
    extra = TOOL_ARGUMENTS.keys() - registered
    assert not missing, f"tools registered but not exercised by this gate: {sorted(missing)}"
    assert not extra, f"tools in this gate that are no longer registered: {sorted(extra)}"


@pytest.mark.parametrize("body", BREAKING_UPSTREAM_BODIES)
@pytest.mark.parametrize("tool_name", sorted(TOOL_ARGUMENTS))
async def test_no_tool_can_emit_a_non_dict(tool_name, body, authed_client):
    """Every tool, against every upstream shape that breaks `-> dict` serialisation."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.route(host="librechat").mock(return_value=httpx.Response(200, json=body))
        async with Client(srv.mcp) as client:
            result = await client.call_tool(
                tool_name, TOOL_ARGUMENTS[tool_name], raise_on_error=False
            )
    assert not result.is_error, (
        f"{tool_name} failed through the MCP layer on upstream body {body!r}: "
        f"{[getattr(c, 'text', c) for c in result.content]}"
    )
    assert isinstance(result.data, dict), f"{tool_name} returned {type(result.data).__name__}"


@pytest.mark.parametrize("tool_name", sorted(TOOL_ARGUMENTS))
async def test_no_tool_raises_through_the_mcp_layer_when_upstream_fails(tool_name, authed_client):
    """An upstream 500 must arrive as a structured error dict, never as a traceback.

    This is vikunja#657's defect measured at the caller's layer: an exception escaping
    a tool reaches MCP as a raw traceback with nothing logged. `_tool_error` exists to
    stop that, and this asserts it holds for every tool rather than for the ones
    somebody remembered to cover.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.route(host="librechat").mock(
            return_value=httpx.Response(500, json={"message": "upstream is down"})
        )
        async with Client(srv.mcp) as client:
            result = await client.call_tool(
                tool_name, TOOL_ARGUMENTS[tool_name], raise_on_error=False
            )
    assert not result.is_error, f"{tool_name} raised instead of returning an error dict"
    assert "error" in result.data, f"{tool_name} returned {result.data!r}, expected an error dict"
