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
from typing import Any, Optional

import structlog
from fastmcp import FastMCP

from .client import LibreChatConfigError, LibreChatError, get_client

log = structlog.get_logger(__name__)

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

def _tool_error(tool: str, err: Exception) -> dict:
    log.error("tool_error", tool=tool, error=str(err))
    return {"error": str(err)}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
async def list_agents(search: str = "", limit: int = 20) -> dict:
    """List LibreChat agents.

    Args:
        search: Optional search string to filter agents by name.
        limit: Maximum number of agents to return (default 20).
    """
    client = get_client()
    try:
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        data = await client.request("GET", "/api/agents", params=params)
        if isinstance(data, list):
            agents = data
        elif isinstance(data, dict):
            agents = data.get("agents", data.get("data", []))
        else:
            agents = []
        log.info("list_agents", count=len(agents), search=search or None)
        return {"agents": agents, "count": len(agents)}
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("list_agents", e)


@mcp.tool
async def get_agent(agent_id: str) -> dict:
    """Get a LibreChat agent by ID.

    Args:
        agent_id: Agent ID from list_agents.
    """
    client = get_client()
    try:
        data = await client.request("GET", f"/api/agents/{agent_id}")
        return data or {}
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("get_agent", e)


@mcp.tool
async def create_agent(
    provider: str,
    model: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    tools: list[str] = [],
    conversation_starters: list[str] = [],
    model_parameters: dict = {},
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
    client = get_client()
    try:
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
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("create_agent", e)


@mcp.tool
async def update_agent(
    agent_id: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    tools: Optional[list[str]] = None,
    conversation_starters: Optional[list[str]] = None,
    model_parameters: Optional[dict] = None,
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
    client = get_client()
    try:
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
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("update_agent", e)


@mcp.tool
async def delete_agent(agent_id: str) -> dict:
    """Delete a LibreChat agent by ID.

    Args:
        agent_id: Agent ID from list_agents or create_agent.
    """
    client = get_client()
    try:
        data = await client.request("DELETE", f"/api/agents/{agent_id}")
        log.info("delete_agent", agent_id=agent_id)
        return data if data is not None else {"deleted": True, "agent_id": agent_id}
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("delete_agent", e)


@mcp.tool
async def list_tools() -> dict:
    """List available LibreChat agent tools and capabilities.

    Returns the set of tool names that can be passed to create_agent or update_agent.
    """
    client = get_client()
    try:
        data = await client.request("GET", "/api/agents/tools")
        return data if data is not None else {}
    except (LibreChatError, LibreChatConfigError) as e:
        return _tool_error("list_tools", e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    port = int(os.getenv("MCP_PORT", "8496"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
