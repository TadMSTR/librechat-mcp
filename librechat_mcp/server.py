"""
librechat-mcp — FastMCP server for LibreChat agent management.

Tools:
  list_agents    — list agents (search, limit)
  get_agent      — get a single agent by ID
  create_agent   — create a new agent
  update_agent   — partial update of an existing agent
  delete_agent   — delete an agent by ID
  list_tools     — list available LibreChat agent capabilities
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog
from fastmcp import FastMCP

from .client import LibreChatConfigError, LibreChatError, get_client

log = structlog.get_logger(__name__)

_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_agent_id(agent_id: str) -> str | None:
    """Return None if valid, error string if invalid."""
    if not agent_id or not _AGENT_ID_RE.match(agent_id):
        return "Invalid agent_id: must match ^[a-zA-Z0-9_-]+$"
    return None


mcp = FastMCP(
    name="librechat-mcp",
    instructions=(
        "LibreChat MCP server. Provides CRUD access to LibreChat agents. "
        "Use list_tools to discover available capabilities before creating agents. "
        "Required fields for create_agent: provider and model. "
        "update_agent supports partial updates — only include fields to change."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Why `get_client()` is called INSIDE each tool's try block rather than above it.
#
# `LibreChatConfigError` is raised there, on first use — and until v0.2.0 the call
# sat outside the block while the `except` clause explicitly named that class. The
# guard was unreachable: it named an exception the block could not see, so a missing
# LIBRECHAT_URL escaped as a traceback exactly like the JSONDecodeError did. Landing
# a guard is not the same as reaching it.
_CLIENT_INSIDE_TRY = "see the note above _tool_error"


def _tool_error(tool: str, err: Exception) -> dict:
    """Turn any exception into a structured error dict. No tool may raise.

    Anything escaping a tool reaches the MCP layer as a raw traceback, and that is
    how vikunja#657 stayed invisible for six weeks: `resp.json()` raised
    `json.JSONDecodeError` on LibreChat's 200-with-an-SSE-error-body, that class was
    not in the old `except (LibreChatError, LibreChatConfigError)` clause, and the
    caller got `Expecting value: line 1 column 1 (char 0)` with nothing logged and
    nothing measured. The clauses are now `except Exception` for exactly that reason.
    """
    expected = isinstance(err, LibreChatError | LibreChatConfigError)
    # An unexpected exception gets a traceback: by definition we did not anticipate
    # it, and the class name alone will not say which line produced it. An expected
    # one does not — its message IS the diagnosis, and a traceback on every failed
    # tool call would bury the signal in the logs meant to surface it.
    log.error(
        "tool_error",
        tool=tool,
        error_type=type(err).__name__,
        error=str(err),
        expected=expected,
        exc_info=not expected,
    )
    # An unexpected error's class is part of the diagnosis, so the caller gets it
    # too; an expected one already names itself. Truncation retained from the F-03
    # fix — error text can carry response bodies and a tool result is model-visible.
    message = str(err) if expected else f"{type(err).__name__}: {err}"
    return {"error": message[:200]}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_agents(search: str = "", limit: int = 20) -> dict:
    """List LibreChat agents.

    Returns a PROJECTION, not full agent objects — only `_id`, `id`, `name`,
    `description`, `category`, `author`, `isPublic`, `is_promoted`,
    `support_contact` and `updatedAt`. There is no `model`, `provider`, `tools` or
    `instructions` here; call `get_agent` for those.

    The result carries `has_more`, and `after` when there are further pages. The
    endpoint is cursor-paginated and this tool does not walk the cursor — one call
    is one page. Reporting the cursor rather than following it keeps the tool a
    single request while removing the silent half of the truncation.

    Args:
        search: Optional search string to filter agents by name.
        limit: Maximum number of agents to return per page (default 20, max 100).
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if search:
            params["search"] = search
        data = await client.request("GET", "/api/agents", params=params)
        if isinstance(data, list):
            agents = data
        elif isinstance(data, dict):
            agents = data.get("agents", data.get("data", []))
        else:
            agents = []
        # Envelope on v0.8.8-rc2: {object, data, first_id, last_id, has_more, after}.
        # `after` is the documented cursor; `last_id` is the fallback for a response
        # that reports has_more without one.
        has_more = bool(data.get("has_more")) if isinstance(data, dict) else False
        result: dict[str, Any] = {
            "agents": agents,
            "count": len(agents),
            "has_more": has_more,
        }
        if has_more:
            result["after"] = data.get("after") or data.get("last_id")
        log.info("list_agents", count=len(agents), search=search or None, has_more=has_more)
        return result
    except Exception as e:
        return _tool_error("list_agents", e)


@mcp.tool
async def get_agent(agent_id: str) -> dict:
    """Get a LibreChat agent by ID.

    Args:
        agent_id: Agent ID from list_agents.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", f"/api/agents/{agent_id}")
        return data or {}
    except Exception as e:
        return _tool_error("get_agent", e)


@mcp.tool
async def create_agent(
    provider: str,
    model: str,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    conversation_starters: list[str] | None = None,
    model_parameters: dict | None = None,
) -> dict:
    """Create a new LibreChat agent.

    Args:
        provider: LLM provider (e.g. 'anthropic').
        model: Model name (e.g. 'claude-sonnet-4-6').
        name: Display name for the agent.
        description: Short description shown in the UI.
        instructions: System prompt / instructions for the agent.
        tools: List of tool capabilities (e.g. ['web_search', 'artifacts']).
        conversation_starters: Suggested starter messages shown in the UI.
        model_parameters: Model-specific parameters (temperature, max_tokens, etc.).
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        body: dict[str, Any] = {"provider": provider, "model": model}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if instructions is not None:
            body["instructions"] = instructions
        if tools:
            body["tools"] = tools
        if conversation_starters:
            body["conversation_starters"] = conversation_starters
        if model_parameters:
            body["model_parameters"] = model_parameters
        data = await client.request("POST", "/api/agents", json=body)
        log.info("create_agent", name=name, provider=provider, model=model)
        return data or {}
    except Exception as e:
        return _tool_error("create_agent", e)


@mcp.tool
async def update_agent(
    agent_id: str,
    provider: str | None = None,
    model: str | None = None,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    conversation_starters: list[str] | None = None,
    model_parameters: dict | None = None,
) -> dict:
    """Update a LibreChat agent (partial update — only include fields to change).

    Args:
        agent_id: Agent ID from list_agents or create_agent.
        provider: LLM provider.
        model: Model name.
        name: Display name.
        description: Short description.
        instructions: System prompt.
        tools: List of tool capabilities (replaces existing list).
        conversation_starters: Suggested starter messages (replaces existing list).
        model_parameters: Model-specific parameters (replaces existing dict).
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        body: dict[str, Any] = {}
        if provider is not None:
            body["provider"] = provider
        if model is not None:
            body["model"] = model
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if instructions is not None:
            body["instructions"] = instructions
        if tools is not None:
            body["tools"] = tools
        if conversation_starters is not None:
            body["conversation_starters"] = conversation_starters
        if model_parameters is not None:
            body["model_parameters"] = model_parameters
        if not body:
            return {"error": "No fields to update — provide at least one field to change"}
        data = await client.request("PATCH", f"/api/agents/{agent_id}", json=body)
        log.info("update_agent", agent_id=agent_id, fields=list(body.keys()))
        return data or {}
    except Exception as e:
        return _tool_error("update_agent", e)


@mcp.tool
async def delete_agent(agent_id: str) -> dict:
    """Delete a LibreChat agent by ID.

    Args:
        agent_id: Agent ID from list_agents or create_agent.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("DELETE", f"/api/agents/{agent_id}")
        log.info("delete_agent", agent_id=agent_id)
        return data if data is not None else {"deleted": True, "agent_id": agent_id}
    except Exception as e:
        return _tool_error("delete_agent", e)


@mcp.tool
async def list_tools() -> dict:
    """List available LibreChat agent tools and capabilities.

    Returns the set of tool names that can be passed to create_agent or update_agent.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", "/api/agents/tools")
        return data if data is not None else {}
    except Exception as e:
        return _tool_error("list_tools", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    port = int(os.getenv("MCP_PORT", "8496"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
