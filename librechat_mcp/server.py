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

import atexit
import hmac
import os
import re
from typing import Any

import structlog
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .client import LibreChatConfigError, LibreChatError, get_client
from .observability import configure_logging, get_tracer, instrument, shutdown_observability

# "librechat-mcp.server", not __name__. __name__ is "librechat_mcp.server" —
# UNDERSCORE — which is not a child of the "librechat-mcp" logger that
# configure_logging() attaches handlers to, so every line from this module would
# propagate to root instead and be emitted by nothing. The dotted name puts it in
# the app logger's hierarchy. tests/test_observability.py asserts it arrives.
log = structlog.get_logger("librechat-mcp.server")

_AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# `Constants.mcp_delimiter` in LibreChat's data-provider package. An MCP tool's
# pluginKey is `${toolName}_mcp_${serverName}`, and it is that suffix — nothing in the
# request body — that makes LibreChat treat a tool as belonging to an MCP server.
_MCP_DELIMITER = "_mcp_"

# Ceiling on the pagination walk in `_collect_agents`. Hitting it is REPORTED to the
# caller as `truncated`, never swallowed — a silent cap is how "list everything"
# quietly becomes "list the first few hundred", which is the defect this replaced.
_MAX_AGENT_PAGES = 20

# Fields LibreChat's agent API ACCEPTS on create/update, read from `agentCreateSchema`
# in the running container's `packages/api/dist/index.d.cts` on v0.8.8-rc2 and then
# round-tripped one by one against the live instance.
#
# **The Mongoose schema is a superset and is not the contract.** `agentCreateSchema`
# and `agentUpdateSchema` are zod schemas in `'strip'` mode, so a key they do not
# declare is dropped SILENTLY and the request still returns 200/201. A field can exist
# in `agent.ts`, store fine in Mongo, and be unsettable over the API.
#
# Measured as silently dropped, and therefore deliberately NOT exposed as parameters:
#   isPublic, is_promoted, access_level — posting them returns 201 and changes nothing.
#     Sharing is done through the ACL surface (`share_agent`), which is the layer that
#     actually works.
#   tool_kwargs — present on the live agent object, absent from the zod schema.
#   mcpServerNames — see `_expand_mcp_servers`; it is DERIVED from `tools`, not set.
_AGENT_WRITABLE_FIELDS = (
    "name",
    "description",
    "instructions",
    "provider",
    "model",
    "model_parameters",
    "tools",
    "conversation_starters",
    "category",
    "memory_scope",
    "artifacts",
    "avatar",
    "recursion_limit",
    "end_after_tools",
    "hide_sequential_outputs",
    "support_contact",
    "agent_ids",
    "edges",
    "subagents",
    "skills",
    "skills_enabled",
    "skills_scope",
    "skill_authoring_enabled",
    "tool_options",
    "tool_resources",
    "code_environment_id",
    "stateful_code_environment",
    "stateful_code_sessions",
)

# HTTP transport is this server's only transport, and the tools behind it are full
# agent CRUD — create, update and delete, against the instance the whole fleet is
# built on. Until v0.2.0 there was no authentication at all: an unauthenticated
# `initialize` returned 200 from a throwaway container on `librechat-internal`, the
# same network the LibreChat app itself sits on. Fail closed.
_MIN_API_TOKEN_LENGTH = 16

# The only path that answers without a bearer token. Exact match against
# scope["path"], never a prefix test: startswith("/health") would also exempt
# /healthz, /health-debug and anything else someone later adds under that stem.
# This is a closed list of one entry, not a namespace.
_AUTH_EXEMPT_PATHS = frozenset({"/health"})


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


def _as_dict(data: Any, *, plural: str) -> dict:
    """Coerce any upstream payload into the dict a `-> dict` tool must return.

    **This is the fix for vikunja#672, and it is a convention rather than a patch.**
    FastMCP builds each tool's output schema from its return annotation and refuses
    to serialise anything that does not match: a `-> dict` tool returning a list
    fails with `structured_content must be a dict or None` — *on a successful
    upstream response*. `list_tools` did exactly that and was 100% dead through MCP
    for the whole of v0.2.0, while its unit test passed because it called the
    undecorated function and so never crossed the boundary that breaks.

    Four endpoints wrapped by this server return bare JSON arrays — measured live on
    v0.8.8-rc2: `/api/agents/tools`, `/api/agents/categories`,
    `/api/agents/<id>/versions` and `/api/permissions/agent/roles`. Each would have
    reproduced #672 verbatim if returned unwrapped, which is why this lands before
    the tools that wrap them rather than after.

    The scalar branch is not defensive padding: a bare string or number breaks
    FastMCP exactly as a list does, and the point of routing every return through one
    helper is that no upstream shape — including one nobody has seen yet — can escape
    as a non-dict. The five tools that shipped in v0.2.0 returning `data or {}` were
    safe only because upstream happens to send objects today. That is a coincidence,
    not a guarantee.
    """
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {plural: data, "count": len(data)}
    return {plural: [data], "count": 1}


async def _mcp_server_tools(client: Any) -> dict[str, list[str]]:
    """Map MCP server name -> its tool pluginKeys, from `GET /api/mcp/tools`.

    NOT from `GET /api/agents/tools`, which lists only LibreChat's 13 built-in tools
    and no MCP tool at all — measured on v0.8.8-rc2. Validating an MCP tool key
    against that endpoint would reject every valid one.
    """
    data = await client.request("GET", "/api/mcp/tools")
    # `isinstance` rather than `(data or {})`: a truthy non-dict — the bare array this
    # API hands back elsewhere — would reach `.get` and raise AttributeError, which
    # then surfaces as an unexpected-error traceback instead of a usable message.
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        name: [t["pluginKey"] for t in (spec.get("tools") or []) if t.get("pluginKey")]
        for name, spec in servers.items()
        if isinstance(spec, dict)
    }


async def _expand_mcp_servers(client: Any, servers: list[str]) -> tuple[list[str], list[str]]:
    """Expand MCP server names into the tool pluginKeys that attach them to an agent.

    **`mcpServerNames` cannot be set, and this is why the tool takes `mcp_servers`
    instead.** It is absent from `agentCreateSchema`/`agentUpdateSchema` and is
    DERIVED server-side in `api/server/controllers/agents/v1.js` (create :880,
    update :1304) from whichever entries of `tools` carry the `_mcp_` delimiter.
    Measured on v0.8.8-rc2:

        posted tools=['search_mcp_searxng'], no mcpServerNames -> mcpServerNames=['searxng']
        posted mcpServerNames=['searxng'], no MCP tools        -> mcpServerNames=[]

    So the honest interface is "name the servers you want" — expanded here into the
    tool keys LibreChat actually reads. Returns `(tool_keys, unknown_servers)`; the
    caller reports the unknown ones rather than posting keys that would be dropped.
    """
    available = await _mcp_server_tools(client)
    keys: list[str] = []
    unknown: list[str] = []
    for name in servers:
        if name in available:
            keys.extend(available[name])
        else:
            unknown.append(name)
    return keys, unknown


async def _collect_agents(
    client: Any, *, search: str, limit: int, follow_cursor: bool
) -> tuple[list[dict], bool, Any, int]:
    """Page through `GET /api/agents`, returning `(agents, has_more, after, pages)`.

    **The response names the cursor `after`; the request parameter is `cursor`.** The
    envelope on v0.8.8-rc2 is `{object, data, first_id, last_id, has_more, after}`,
    but `api/server/controllers/agents/v1.js:1663` reads
    `const { category, search, limit, cursor, promoted } = req.query`. Sending it back
    as `after=` is an unknown query parameter, so it is ignored and every page is page
    one — with `has_more: true` and an identical `after`, forever. Measured: `after=`
    returned the same agent twice; `cursor=` walked the list correctly.

    That asymmetry is the same silent-drop shape as everything else in this API —
    the wrong key is discarded rather than refused — and it would be an infinite loop
    for a client that trusted `has_more`.

    Two independent stops, because one of them is load-bearing on any given day:
    `_MAX_AGENT_PAGES` bounds the walk, and a page yielding no new ids breaks it. The
    no-progress guard is what caught the parameter-name bug on the first live run
    instead of hanging. Hitting either is REPORTED by the caller, never swallowed — a
    silent cap turns "list everything" into a wrong answer.
    """
    params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
    if search:
        params["search"] = search

    agents: list[dict] = []
    seen: set[str] = set()
    after: Any = None
    has_more = False
    pages = 0

    while pages < _MAX_AGENT_PAGES:
        page_params = dict(params)
        if after:
            # `cursor`, NOT `after` — see the docstring. This one word is the
            # difference between paging and looping on page one.
            page_params["cursor"] = after
        data = await client.request("GET", "/api/agents", params=page_params)
        pages += 1

        if isinstance(data, list):
            page = data
            has_more = False
        elif isinstance(data, dict):
            page = data.get("data") or data.get("agents") or []
            has_more = bool(data.get("has_more"))
            # `after` only. `last_id` is an agent id, not a cursor: passing it as
            # `cursor` re-returns the same page, so a fallback to it would silently
            # convert "no cursor" into "loop on page one".
            after = data.get("after")
        else:
            page, has_more = [], False

        fresh = [
            a for a in page if isinstance(a, dict) and (a.get("id") or a.get("_id")) not in seen
        ]
        seen.update(str(a.get("id") or a.get("_id")) for a in fresh)
        agents.extend(fresh)

        if not follow_cursor or not has_more or not after or not fresh:
            break

    return agents, has_more, after, pages


def _spec_differences(desired: dict, current: dict) -> list[str]:
    """Field names where `desired` and `current` disagree.

    Only fields the caller actually specified are compared, so an unspecified field
    is "don't care" rather than "set to null" — which is what makes a partial spec
    safe to re-apply.

    Comparison is order-insensitive for `tools` and `mcpServerNames`: LibreChat
    returns MCP tool keys in its own resolved order, so a positional compare would
    report a difference on every run and `unchanged` would never be reachable.
    """
    unordered = {"tools", "mcpServerNames", "agent_ids", "skills"}
    changed = []
    for key, want in desired.items():
        have = current.get(key)
        if key in unordered and isinstance(want, list) and isinstance(have, list):
            if sorted(map(str, want)) != sorted(map(str, have)):
                changed.append(key)
        elif want != have:
            changed.append(key)
    return changed


def _dropped_tools(requested: list[str] | None, returned: Any) -> list[str]:
    """Tools that were asked for and did not survive the write.

    **LibreChat drops an unknown or unauthorized tool silently on a 201.** Measured:
    posting `tools=['search_mcp_nosuchserver']` returns 201 with `tools: []` and no
    error anywhere in the response. `filterAuthorizedTools` removes it and the request
    still succeeds.

    That is this stack's house failure mode — a success-shaped signal — and it is
    exactly what a caller provisioning a fleet must not miss, because the agent is
    then created without the capability it was created for. Surfacing the difference
    turns a silent drop into a visible one.
    """
    if not requested or not isinstance(returned, dict):
        return []
    kept = returned.get("tools")
    if not isinstance(kept, list):
        return []
    return [t for t in requested if t not in kept]


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
@instrument("list_agents")
async def list_agents(
    search: str = "",
    limit: int = 20,
    expand: bool = False,
    follow_cursor: bool = True,
) -> dict:
    """List LibreChat agents.

    Returns a PROJECTION, not full agent objects — only `_id`, `id`, `name`,
    `description`, `category`, `author`, `isPublic`, `is_promoted`,
    `support_contact` and `updatedAt`. There is no `model`, `provider`, `tools` or
    `instructions` here; call `get_agent` for those.

    Follows the cursor by default, so the result is every matching agent rather than
    the first page. `truncated: true` in the result means the page ceiling was hit
    before the cursor ran out — a bounded, *reported* truncation, unlike the silent
    one this replaced.

    Args:
        search: Optional search string to filter agents by name.
        limit: Page size (default 20, max 100). Not a cap on the total returned.
        expand: Fetch the full object for each agent with one `get_agent` per result.
            Off by default because it is an N+1 fan-out. Turn it on when you need
            something to diff a desired spec against — `ensure_agent` does exactly
            this internally.
        follow_cursor: Walk the pagination cursor. Off gives a single page plus
            `has_more`/`after`.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        agents, has_more, after, pages = await _collect_agents(
            client, search=search, limit=limit, follow_cursor=follow_cursor
        )
        result: dict[str, Any] = {
            "agents": agents,
            "count": len(agents),
            "has_more": has_more,
        }
        if has_more:
            result["after"] = after
            if follow_cursor:
                result["truncated"] = True
                result["warning"] = (
                    f"Stopped after {_MAX_AGENT_PAGES} pages with more results "
                    "available. Narrow the search rather than trusting this count."
                )
        if expand:
            result["agents"] = [
                _as_dict(await client.request("GET", f"/api/agents/{a['id']}"), plural="agents")
                for a in agents
                if isinstance(a, dict) and a.get("id")
            ]
            result["expanded"] = True
        log.info(
            "list_agents",
            count=len(agents),
            search=search or None,
            has_more=has_more,
            pages=pages,
            expand=expand,
        )
        return result
    except Exception as e:
        return _tool_error("list_agents", e)


@mcp.tool
@instrument("get_agent")
async def get_agent(agent_id: str, expanded: bool = False) -> dict:
    """Get a LibreChat agent by ID.

    Only fields that are SET come back. An absent key means unset, not unsupported —
    the live `Web Search` agent returns 21 of the 38 schema fields. Do not infer what
    the API accepts from what a read returns.

    Args:
        agent_id: Agent ID from list_agents.
        expanded: Use `/expanded`. **This is a permission tier, not extra fields.**
            The plain read needs `PermissionBits.VIEW` and returns non-sensitive data;
            `/expanded` needs **EDIT** and returns complete configuration. So it is a
            disclosure surface, and a VIEW-only caller gets a 403 rather than a
            thinner payload.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        path = f"/api/agents/{agent_id}/expanded" if expanded else f"/api/agents/{agent_id}"
        data = await client.request("GET", path)
        return _as_dict(data, plural="agents")
    except Exception as e:
        return _tool_error("get_agent", e)


@mcp.tool
@instrument("duplicate_agent")
async def duplicate_agent(agent_id: str) -> dict:
    """Copy an agent, including its instructions and tools.

    The natural primitive for templating a family agent from a known-good one, and
    the cheap way to get a scratch agent to experiment against. The copy gets a new
    id and a name suffixed with a timestamp.

    Requires `AGENTS.USE` + `AGENTS.CREATE` on the role and `PermissionBits.EDIT` on
    the source agent. The copy does NOT inherit the source's ACL grants — it is owned
    by the caller and shared with nobody; re-share it with share_agent.

    Args:
        agent_id: Agent ID to copy.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("POST", f"/api/agents/{agent_id}/duplicate")
        result = _as_dict(data, plural="agents")
        log.info("duplicate_agent", source=agent_id, new_id=(result.get("agent") or {}).get("id"))
        return result
    except Exception as e:
        return _tool_error("duplicate_agent", e)


@mcp.tool
@instrument("list_agent_versions")
async def list_agent_versions(agent_id: str) -> dict:
    """List an agent's saved versions, oldest first.

    Returns `{"versions": [...], "count": N}`. The position in this list is the
    `version_index` that revert_agent takes.

    `get_agent` does **not** include `versions` — history is loaded lazily upstream so
    the editor need not transfer it — which is why this is its own route rather than a
    field read. Requires `PermissionBits.EDIT`.

    Args:
        agent_id: Agent ID from list_agents.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", f"/api/agents/{agent_id}/versions")
        return _as_dict(data, plural="versions")
    except Exception as e:
        return _tool_error("list_agent_versions", e)


@mcp.tool
@instrument("revert_agent")
async def revert_agent(agent_id: str, version_index: int) -> dict:
    """Restore an agent to one of its saved versions.

    Without this, a programmatic prompt edit is one-way. `version_index` is a
    **numeric index into list_agent_versions**, not a version id or name — LibreChat
    rejects any other body key with `400 version_index is required`.

    Reverting overwrites the agent's current state. Read list_agent_versions first
    and confirm the index is the one you want.

    Args:
        agent_id: Agent ID from list_agents.
        version_index: Zero-based index into list_agent_versions.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    if not isinstance(version_index, int) or isinstance(version_index, bool):
        return {"error": "version_index must be an integer index into list_agent_versions"}
    if version_index < 0:
        return {"error": "version_index must be >= 0"}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request(
            "POST", f"/api/agents/{agent_id}/revert", json={"version_index": version_index}
        )
        log.warning("revert_agent", agent_id=agent_id, version_index=version_index)
        return _as_dict(data, plural="agents")
    except Exception as e:
        return _tool_error("revert_agent", e)


@mcp.tool
@instrument("create_agent")
async def create_agent(
    provider: str,
    model: str,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    conversation_starters: list[str] | None = None,
    model_parameters: dict | None = None,
    category: str | None = None,
    memory_scope: str | None = None,
    artifacts: str | None = None,
    avatar: dict | None = None,
    recursion_limit: int | None = None,
    end_after_tools: bool | None = None,
    hide_sequential_outputs: bool | None = None,
    support_contact: dict | None = None,
    agent_ids: list[str] | None = None,
    edges: list[dict] | None = None,
    subagents: list[dict] | None = None,
    skills: list[str] | None = None,
    skills_enabled: bool | None = None,
    skills_scope: str | None = None,
    skill_authoring_enabled: bool | None = None,
    tool_options: dict | None = None,
    tool_resources: dict | None = None,
    code_environment_id: str | None = None,
    stateful_code_environment: str | None = None,
    stateful_code_sessions: bool | None = None,
) -> dict:
    """Create a new LibreChat agent.

    Returns the created agent. When tools were requested and LibreChat kept only some
    of them, the result also carries `dropped_tools` and `warning` — see below, and do
    not ignore them: a drop is reported on a **201**.

    Args:
        provider: Endpoint name. For a custom endpoint this is the endpoint's `name:`
            from librechat.yaml verbatim and case-sensitive — on forge, `Mistral`.
        model: Model id, e.g. 'mistral-small-latest'. Validate with list_models.
        name: Display name. Not unique server-side; two agents may share one.
        description: Short description shown in the UI.
        instructions: System prompt.
        tools: Tool pluginKeys — `pluginKey`, not the display `name`. Use list_tools
            for built-ins. **Unknown or unauthorized keys are dropped silently on a
            201**, so check `dropped_tools` in the result.
        mcp_servers: MCP server names to attach, e.g. ['searxng']. Expanded here into
            that server's tool pluginKeys and appended to `tools`, because
            **`mcpServerNames` is derived by LibreChat from `tools` and cannot be
            set directly** — posting it is silently ignored. Discover names with
            list_mcp_servers.
        conversation_starters: Suggested starter messages shown in the UI.
        model_parameters: Model parameters (temperature, max_tokens, …).
        category: Agent-picker grouping. Valid values from list_categories.
        memory_scope: **'agent' or 'user'** — NOT a boolean. 'agent' is the Agent
            Builder's "Keep memories separate for this agent"; omitted means the
            shared pool. This is per-agent memory isolation, new in v0.8.8.
        artifacts: Per-agent artifact behaviour, e.g. 'default'.
        avatar: Avatar object, `{filepath, source}`.
        recursion_limit: Bounds a delegating agent's recursion.
        end_after_tools: Stop the turn after tool execution.
        hide_sequential_outputs: Hide intermediate outputs of sequential tools.
        support_contact: `{name, email}`. The email is validated server-side — a
            malformed address is a 400, not a silent drop.
        agent_ids: **Deprecated upstream — use `edges`.** Kept for compatibility.
        edges: Delegation graph entries,
            `{from, to, description?, edgeType?: 'handoff'|'direct', prompt?,
            excludeResults?, promptKey?}`.
        subagents: Subagent members. Capped by `endpoints.agents.maxSubagents`
            (default 10), with server-side topology validation.
        skills: Agent Skills entries.
        skills_enabled: Enable Agent Skills for this agent.
        skills_scope: **'all', 'selected' or 'none'** — do not confuse with
            `memory_scope`, whose values are different.
        skill_authoring_enabled: Allow this agent to author skills.
        tool_options: `tool_id -> {defer_loading?, allowed_callers?,
            run_in_background?, describe_intent?}`.
        tool_resources: Only `image_edit`, `execute_code`, `file_search`, `context`
            and `ocr` (deprecated) are accepted.
        code_environment_id: Bind the agent to a code environment.
        stateful_code_environment: 'user', 'agent-user' or 'conversation'.
        stateful_code_sessions: Persist code sessions across turns.

    Not exposed, because LibreChat accepts them and silently ignores them (measured —
    each returns 201 and changes nothing): `isPublic`, `is_promoted`, `access_level`,
    `tool_kwargs`. Share an agent with `share_agent`, which uses the ACL surface that
    actually works.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        body: dict[str, Any] = {"provider": provider, "model": model}
        supplied = {
            "name": name,
            "description": description,
            "instructions": instructions,
            "tools": tools,
            "conversation_starters": conversation_starters,
            "model_parameters": model_parameters,
            "category": category,
            "memory_scope": memory_scope,
            "artifacts": artifacts,
            "avatar": avatar,
            "recursion_limit": recursion_limit,
            "end_after_tools": end_after_tools,
            "hide_sequential_outputs": hide_sequential_outputs,
            "support_contact": support_contact,
            "agent_ids": agent_ids,
            "edges": edges,
            "subagents": subagents,
            "skills": skills,
            "skills_enabled": skills_enabled,
            "skills_scope": skills_scope,
            "skill_authoring_enabled": skill_authoring_enabled,
            "tool_options": tool_options,
            "tool_resources": tool_resources,
            "code_environment_id": code_environment_id,
            "stateful_code_environment": stateful_code_environment,
            "stateful_code_sessions": stateful_code_sessions,
        }
        body.update({k: v for k, v in supplied.items() if v is not None})

        unknown_servers: list[str] = []
        if mcp_servers:
            mcp_keys, unknown_servers = await _expand_mcp_servers(client, mcp_servers)
            if unknown_servers:
                return {
                    "error": (
                        f"Unknown MCP server(s): {', '.join(unknown_servers)}. "
                        "Use list_mcp_servers to see what LibreChat has enumerated."
                    )
                }
            body["tools"] = [*(body.get("tools") or []), *mcp_keys]

        requested_tools = body.get("tools")
        data = await client.request("POST", "/api/agents", json=body)
        result = _as_dict(data, plural="agents")
        dropped = _dropped_tools(requested_tools, result)
        if dropped:
            result["dropped_tools"] = dropped
            result["warning"] = (
                f"LibreChat silently dropped {len(dropped)} requested tool(s) on a "
                "successful create — they are unknown or not authorised for this "
                "account. The agent exists but lacks those capabilities."
            )
        log.info(
            "create_agent",
            name=name,
            provider=provider,
            model=model,
            fields=sorted(body.keys()),
            mcp_servers=mcp_servers or None,
            dropped_tools=dropped or None,
        )
        return result
    except Exception as e:
        return _tool_error("create_agent", e)


@mcp.tool
@instrument("update_agent")
async def update_agent(
    agent_id: str,
    provider: str | None = None,
    model: str | None = None,
    name: str | None = None,
    description: str | None = None,
    instructions: str | None = None,
    tools: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    conversation_starters: list[str] | None = None,
    model_parameters: dict | None = None,
    category: str | None = None,
    memory_scope: str | None = None,
    artifacts: str | None = None,
    avatar: dict | None = None,
    recursion_limit: int | None = None,
    end_after_tools: bool | None = None,
    hide_sequential_outputs: bool | None = None,
    support_contact: dict | None = None,
    agent_ids: list[str] | None = None,
    edges: list[dict] | None = None,
    subagents: list[dict] | None = None,
    skills: list[str] | None = None,
    skills_enabled: bool | None = None,
    skills_scope: str | None = None,
    skill_authoring_enabled: bool | None = None,
    tool_options: dict | None = None,
    tool_resources: dict | None = None,
    code_environment_id: str | None = None,
    stateful_code_environment: str | None = None,
    stateful_code_sessions: bool | None = None,
) -> dict:
    """Partially update a LibreChat agent — only supplied fields are sent.

    **List and dict fields REPLACE, they do not merge.** `tools=['calculator']` on an
    agent that had three tools leaves it with one. To add a tool, read the current
    list with get_agent and post the union.

    A no-op update is safe: LibreChat compares against the current state and does not
    append to `versions[]` when nothing changed (measured on v0.8.8-rc2), so
    reconciliation does not pollute the revert history.

    Args:
        agent_id: Agent ID from list_agents or create_agent.

    All other arguments are as documented on create_agent, including the
    `mcp_servers` note — `mcpServerNames` is derived from `tools` on update too and
    cannot be set directly. Passing `mcp_servers` REPLACES the agent's tool list with
    the expansion (plus any `tools` given alongside), which is how detaching a server
    works: omit it and its tools go, and LibreChat revokes the agent-scoped access.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        supplied = {
            "provider": provider,
            "model": model,
            "name": name,
            "description": description,
            "instructions": instructions,
            "tools": tools,
            "conversation_starters": conversation_starters,
            "model_parameters": model_parameters,
            "category": category,
            "memory_scope": memory_scope,
            "artifacts": artifacts,
            "avatar": avatar,
            "recursion_limit": recursion_limit,
            "end_after_tools": end_after_tools,
            "hide_sequential_outputs": hide_sequential_outputs,
            "support_contact": support_contact,
            "agent_ids": agent_ids,
            "edges": edges,
            "subagents": subagents,
            "skills": skills,
            "skills_enabled": skills_enabled,
            "skills_scope": skills_scope,
            "skill_authoring_enabled": skill_authoring_enabled,
            "tool_options": tool_options,
            "tool_resources": tool_resources,
            "code_environment_id": code_environment_id,
            "stateful_code_environment": stateful_code_environment,
            "stateful_code_sessions": stateful_code_sessions,
        }
        body: dict[str, Any] = {k: v for k, v in supplied.items() if v is not None}

        if mcp_servers is not None:
            mcp_keys, unknown_servers = await _expand_mcp_servers(client, mcp_servers)
            if unknown_servers:
                return {
                    "error": (
                        f"Unknown MCP server(s): {', '.join(unknown_servers)}. "
                        "Use list_mcp_servers to see what LibreChat has enumerated."
                    )
                }
            body["tools"] = [*(body.get("tools") or []), *mcp_keys]

        if not body:
            return {"error": "No fields to update — provide at least one field to change"}

        requested_tools = body.get("tools")
        data = await client.request("PATCH", f"/api/agents/{agent_id}", json=body)
        result = _as_dict(data, plural="agents")
        dropped = _dropped_tools(requested_tools, result)
        if dropped:
            result["dropped_tools"] = dropped
            result["warning"] = (
                f"LibreChat silently dropped {len(dropped)} requested tool(s) on a "
                "successful update — they are unknown or not authorised for this "
                "account."
            )
        log.info(
            "update_agent",
            agent_id=agent_id,
            fields=sorted(body.keys()),
            mcp_servers=mcp_servers,
            dropped_tools=dropped or None,
        )
        return result
    except Exception as e:
        return _tool_error("update_agent", e)


@mcp.tool
@instrument("delete_agent")
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
        if data is None:
            return {"deleted": True, "agent_id": agent_id}
        return _as_dict(data, plural="deleted")
    except Exception as e:
        return _tool_error("delete_agent", e)


@mcp.tool
@instrument("list_tools")
async def list_tools() -> dict:
    """List LibreChat's BUILT-IN agent tools.

    Returns `{"tools": [...], "count": N}`. Each entry carries a `pluginKey`, and it
    is the `pluginKey` — not the display `name` — that goes in `create_agent(tools=)`.

    **This does not list MCP server tools.** Measured on v0.8.8-rc2: this endpoint
    returns 13 built-in tools (`calculator`, `google`, `dalle`, …) and no MCP tool at
    all. To attach an MCP server to an agent, use `list_mcp_tools` and pass those
    `pluginKey`s — see the note on `create_agent(mcp_servers=)`.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", "/api/agents/tools")
        return _as_dict(data, plural="tools")
    except Exception as e:
        return _tool_error("list_tools", e)


@mcp.tool
@instrument("list_mcp_tools")
async def list_mcp_tools(server: str = "") -> dict:
    """List the MCP server tools an agent can be given, keyed by server name.

    Returns `{"servers": {<name>: [pluginKey, ...]}, "count": N}`. These pluginKeys —
    shaped `${toolName}_mcp_${serverName}` — are what `create_agent(tools=)` takes,
    and passing them is what makes LibreChat derive `mcpServerNames`.

    Distinct from `list_tools`, which covers LibreChat's built-in tools only and lists
    no MCP tool at all. Most callers should use `create_agent(mcp_servers=['searxng'])`
    and let it expand these keys.

    Args:
        server: Restrict to one server name. Empty returns every enumerated server.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        by_server = await _mcp_server_tools(client)
        if server:
            if server not in by_server:
                return {
                    "error": (
                        f"Unknown MCP server: {server}. "
                        f"LibreChat has enumerated: {', '.join(sorted(by_server)) or '(none)'}"
                    )
                }
            by_server = {server: by_server[server]}
        return {"servers": by_server, "count": sum(len(v) for v in by_server.values())}
    except Exception as e:
        return _tool_error("list_mcp_tools", e)


@mcp.tool
@instrument("list_mcp_servers")
async def list_mcp_servers() -> dict:
    """List LibreChat's configured MCP servers with their connection state.

    Returns `{"servers": [{name, connectionState, tool_count, ...}], "count": N}`.

    **`connectionState: "disconnected"` is the normal idle state and is not, on its
    own, a fault** — a server can read `disconnected` while a tool call through it
    succeeds. Judge it against `tool_count`: a server enumerating tools is usable.

    Known live discrepancy (vikunja#662): a server whose `headers` block in
    librechat.yaml carries a `{{...}}` placeholder gets no `toolFunctions` in
    LibreChat's registry, so it cannot deliver tools at chat time even where the tool
    catalogue still lists them. On forge that is `jobsearch`; `searxng` is unaffected.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        configured = await client.request("GET", "/api/mcp/servers")
        status = await client.request("GET", "/api/mcp/connection/status")
        if not isinstance(configured, dict):
            return {"servers": [], "count": 0}
        states = status.get("connectionStatus") if isinstance(status, dict) else None
        states = states if isinstance(states, dict) else {}
        by_server = await _mcp_server_tools(client)
        servers = [
            {
                "name": name,
                "connectionState": (states.get(name) or {}).get("connectionState"),
                "requiresOAuth": spec.get("requiresOAuth"),
                "transport": spec.get("type"),
                "source": spec.get("source"),
                "tool_count": len(by_server.get(name, [])),
            }
            for name, spec in configured.items()
            if isinstance(spec, dict)
        ]
        return {"servers": servers, "count": len(servers)}
    except Exception as e:
        return _tool_error("list_mcp_servers", e)


# ---------------------------------------------------------------------------
# RBAC — resource ACLs and role permissions
# ---------------------------------------------------------------------------

# `GET /api/permissions/<type>/roles` on v0.8.8-rc2. Kept only as the fallback for a
# lookup failure: `get_resource_access_roles` reads the live vocabulary, and
# hardcoding grant names is what this server is trying to stop callers doing.
_FALLBACK_AGENT_ACCESS_ROLES = ("agent_viewer", "agent_editor", "agent_owner")

# `ResourceType` in LibreChat. Anything else is a 400 `Unsupported resource type`,
# so validating client-side lets the error name the valid values.
_RESOURCE_TYPES = (
    "agent",
    "remoteAgent",
    "promptGroup",
    "mcpServer",
    "skill",
    "codeEnvironment",
    "sharedLink",
)

# The eight per-type role endpoints under `PUT /api/roles/:roleName/*`, each gated by
# MANAGE_ROLES and each merging server-side. There is no matrix endpoint.
#
# The mapping matters because the URL segment and the key in the role's `permissions`
# object are NOT the same string: `people-picker` is `PEOPLE_PICKER`, `mcp-servers` is
# `MCP_SERVERS`. Reading a permission back after setting it needs both.
_ROLE_PERMISSION_TYPES = {
    "prompts": "PROMPTS",
    "agents": "AGENTS",
    "memories": "MEMORIES",
    "people-picker": "PEOPLE_PICKER",
    "mcp-servers": "MCP_SERVERS",
    "marketplace": "MARKETPLACE",
    "remote-agents": "REMOTE_AGENTS",
    "skills": "SKILLS",
}

# Readable in `GET /api/roles/<ROLE>` but with no write endpoint on that router.
# Offering a tool that pretends to set them would report success and do nothing.
_READ_ONLY_ROLE_PERMISSIONS = (
    "WEB_SEARCH",
    "RUN_CODE",
    "FILE_SEARCH",
    "FILE_CITATIONS",
    "MULTI_CONVO",
    "TEMPORARY_CHAT",
    "BOOKMARKS",
)


async def _resolve_resource_id(client: Any, agent_id: str) -> str:
    """Translate an agent's public `id` into the Mongo `_id` the ACL routes require.

    **This is not a convenience — without it the ACL tools return confident wrong
    answers.** Every `/api/permissions/*` route is keyed by the Mongo `_id`, while
    every other tool here speaks the public `agent_...` id that `list_agents` returns.
    Measured on v0.8.8-rc2 with an agent that had two principals:

        GET /api/permissions/agent/<agent_id>            -> 200 {principals: [], public: false}
        GET /api/permissions/agent/<_id>                 -> 200 {principals: [2 real entries]}
        GET /api/permissions/agent/<agent_id>/effective  -> {permissionBits: 0}
        GET /api/permissions/agent/<_id>/effective       -> {permissionBits: 15}
        PUT /api/permissions/agent/<agent_id>            -> 400 Invalid resource ID

    Only the PUT fails honestly. Both GETs answer 200 with an empty or zero result, so
    a caller passing the id it holds everywhere else is told "nobody has access" about
    an agent it owns. The programme's prerequisite recording this route as verified was
    reading exactly that empty result.

    Exposing `_id` to callers instead would move the trap rather than close it.
    """
    agent = await client.request("GET", f"/api/agents/{agent_id}")
    resource_id = agent.get("_id") if isinstance(agent, dict) else None
    if not resource_id:
        raise LibreChatError(404, f"Agent {agent_id} has no _id — cannot address its permissions")
    return str(resource_id)


@mcp.tool
@instrument("get_agent_permissions")
async def get_agent_permissions(agent_id: str) -> dict:
    """Who can use this agent — the resource ACL.

    Returns `{resourceType, resourceId, principals[], public, agent_id}`. Each
    principal carries an `accessRoleId` (`agent_viewer`, `agent_editor`,
    `agent_owner`); discover the live vocabulary with get_resource_access_roles.

    Takes the public agent id and resolves the Mongo `_id` internally, because the
    permissions routes are keyed by `_id` and answer **200 with an empty
    `principals` list** for the public id rather than erroring.

    Note that an agent document also carries its own `isPublic` flag, which is a
    separate field from this `public` and can disagree with it. Neither this tool nor
    create_agent sets `isPublic` — it is not settable over the API.

    Args:
        agent_id: Agent ID from list_agents or create_agent.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        resource_id = await _resolve_resource_id(client, agent_id)
        data = await client.request("GET", f"/api/permissions/agent/{resource_id}")
        result = _as_dict(data, plural="principals")
        result["agent_id"] = agent_id
        return result
    except Exception as e:
        return _tool_error("get_agent_permissions", e)


@mcp.tool
@instrument("share_agent")
async def share_agent(
    agent_id: str,
    grant: list[dict] | None = None,
    revoke: list[dict] | None = None,
    public_access_role_id: str | None = None,
) -> dict:
    """Grant or revoke access to an agent.

    Grants are named by **`accessRoleId` strings**, not by a permission bitmask —
    `permBits` is LibreChat's internal representation and is not what you post. Get
    the valid ids from get_resource_access_roles.

    Grant and revoke are separate arguments because LibreChat's body has separate
    `updated` and `removed` arrays: omitting a principal does NOT revoke it. Revoking
    is an explicit act.

    **Two consequences worth reading before granting.** Editor and Owner grantees can
    see the agent's system instructions, attached files and tools — sharing an agent
    discloses its configuration, not just its use. And an agent's memory partition is
    anchored to agent access: revoking a share can strand that user's memories for
    this agent, and they are not returned by re-granting.

    `public_access_role_id` defaults to None, meaning "leave the public setting
    alone". Pass it explicitly to change it — it is gated separately server-side by
    `checkSharePublicAccess` and a 403 here means the account lacks SHARE_PUBLIC.

    Args:
        agent_id: Agent ID from list_agents or create_agent.
        grant: Principals to grant, each `{"type": "user"|"group"|"role"|"public",
            "id": "<principal id>", "accessRoleId": "agent_viewer"}`. Find user ids
            with search_principals.
        revoke: Principals to remove, each `{"type": ..., "id": ...}`.
        public_access_role_id: Access role for public access. Explicit only.
    """
    if err := _validate_agent_id(agent_id):
        return {"error": err}
    if not grant and not revoke and public_access_role_id is None:
        return {"error": "Nothing to do — provide grant, revoke or public_access_role_id"}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        resource_id = await _resolve_resource_id(client, agent_id)
        body: dict[str, Any] = {"updated": grant or [], "removed": revoke or []}
        if public_access_role_id is not None:
            body["publicAccessRoleId"] = public_access_role_id
        data = await client.request("PUT", f"/api/permissions/agent/{resource_id}", json=body)
        result = _as_dict(data, plural="results")
        result["agent_id"] = agent_id
        if revoke:
            result["note"] = (
                "Revoking access can strand that principal's memory partition for this "
                "agent — partitions are anchored to agent access and are not restored "
                "by re-granting."
            )
        log.info(
            "share_agent",
            agent_id=agent_id,
            granted=len(grant or []),
            revoked=len(revoke or []),
            public_access_role_id=public_access_role_id,
        )
        return result
    except Exception as e:
        return _tool_error("share_agent", e)


@mcp.tool
@instrument("search_principals")
async def search_principals(query: str, limit: int = 20) -> dict:
    """Find users, groups and roles to grant agent access to — the people picker.

    Returns `{results: [{id, type, name, email, ...}], count, ...}`. The `id` and
    `type` of a result are what share_agent's `grant` takes.

    Gated server-side by the caller's `PEOPLE_PICKER` permissions
    (`VIEW_USERS`/`VIEW_GROUPS`/`VIEW_ROLES`), which are false on the `USER` role by
    default. An account without them gets no results rather than an error.

    Args:
        query: Name or email fragment to search for.
        limit: Maximum results (default 20).
    """
    if not query:
        return {"error": "query is required"}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request(
            "GET", "/api/permissions/search-principals", params={"q": query, "limit": limit}
        )
        return _as_dict(data, plural="results")
    except Exception as e:
        return _tool_error("search_principals", e)


@mcp.tool
@instrument("get_resource_access_roles")
async def get_resource_access_roles(resource_type: str = "agent") -> dict:
    """List the `accessRoleId` values a resource type supports.

    Returns `{"roles": [{accessRoleId, name, description, permBits}], "count": N}`.
    Use this rather than hardcoding Viewer/Editor/Owner — the ids are what
    share_agent posts.

    Args:
        resource_type: One of agent, remoteAgent, promptGroup, mcpServer, skill,
            codeEnvironment, sharedLink. The vocabulary is closed; anything else is a
            400 upstream, so it is rejected here with the valid values named.
    """
    if resource_type not in _RESOURCE_TYPES:
        return {
            "error": (
                f"Unsupported resource_type: {resource_type}. "
                f"Valid values: {', '.join(_RESOURCE_TYPES)}"
            )
        }
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", f"/api/permissions/{resource_type}/roles")
        return _as_dict(data, plural="roles")
    except Exception as e:
        return _tool_error("get_resource_access_roles", e)


@mcp.tool
@instrument("get_effective_permissions")
async def get_effective_permissions(agent_id: str = "") -> dict:
    """What the calling account can actually do — with one agent, or with all of them.

    With `agent_id`, returns `{permissionBits, agent_id}` for that agent. Without it,
    returns `{permissions: {<mongo _id>: <bits>}, count}` for every agent the account
    can see.

    `permissionBits` is a bitmask: 1 = view, 3 = edit, 15 = owner. It maps to the
    `permBits` in get_resource_access_roles.

    **The all-agents form is keyed by the Mongo `_id`, not the public agent id** —
    that is LibreChat's shape and is left as it comes rather than silently rewritten,
    since remapping would need one get_agent per entry. Use the single-agent form when
    you have an agent id.

    Args:
        agent_id: Optional. Empty returns the map for every visible agent.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        if agent_id:
            if err := _validate_agent_id(agent_id):
                return {"error": err}
            resource_id = await _resolve_resource_id(client, agent_id)
            data = await client.request("GET", f"/api/permissions/agent/{resource_id}/effective")
            result = _as_dict(data, plural="permissions")
            result["agent_id"] = agent_id
            return result
        data = await client.request("GET", "/api/permissions/agent/effective/all")
        if isinstance(data, dict):
            return {"permissions": data, "count": len(data)}
        return _as_dict(data, plural="permissions")
    except Exception as e:
        return _tool_error("get_effective_permissions", e)


@mcp.tool
@instrument("get_role_permissions")
async def get_role_permissions(role: str) -> dict:
    """Read a role's full feature matrix.

    Returns the role, including a `permissions` object keyed by permission type
    (`AGENTS`, `MEMORIES`, `MCP_SERVERS`, `WEB_SEARCH`, …). This is the
    "web search on for adults, off for the kids" layer.

    Only some of these are writable — see set_role_permissions. The response also
    reports which, under `writable_permission_types`.

    Args:
        role: Role name, e.g. 'USER' or 'ADMIN'. Case-sensitive.
    """
    if not role:
        return {"error": "role is required"}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", f"/api/roles/{role}")
        result = _as_dict(data, plural="roles")
        result["writable_permission_types"] = sorted(_ROLE_PERMISSION_TYPES)
        result["read_only_permissions"] = list(_READ_ONLY_ROLE_PERMISSIONS)
        return result
    except Exception as e:
        return _tool_error("get_role_permissions", e)


@mcp.tool
@instrument("set_role_permissions")
async def set_role_permissions(role: str, permission_type: str, permissions: dict) -> dict:
    """Change one permission type on a role. Affects EVERY user holding that role.

    One permission type per call, deliberately. On a family instance a role is most
    of the user base, and this is the widest-blast-radius tool here — narrowing it to
    a single type keeps a mistake small and the log line readable. The before and
    after are both logged and both returned.

    **The merge is the server's, not this tool's.** LibreChat parses the body with
    `schema.partial()` and merges into the role's existing permissions, so a partial
    body is the intended usage. Doing a read-modify-write here instead would add a
    lost-update window the API does not have.

    **Unknown permission KEYS are silently dropped by upstream on a 200** — measured:
    `{"NOT_A_REAL_PERMISSION": true}` returns 200 and changes nothing. So keys are
    validated here against what the role actually carries, and an unrecognised one is
    refused rather than reported as applied. A wrong *type* does error upstream (400
    with a usable zod message) and is passed through.

    Args:
        role: Role name, e.g. 'USER'. Case-sensitive.
        permission_type: One of prompts, agents, memories, people-picker,
            mcp-servers, marketplace, remote-agents, skills. These are the eight with
            a write endpoint; WEB_SEARCH, RUN_CODE, FILE_SEARCH, FILE_CITATIONS,
            MULTI_CONVO, TEMPORARY_CHAT and BOOKMARKS are readable but NOT writable,
            and are refused here rather than silently ignored.
        permissions: Partial permission object, e.g. {"SHARE": true}. Merged
            server-side into the role's current values.
    """
    if not role:
        return {"error": "role is required"}
    if permission_type not in _ROLE_PERMISSION_TYPES:
        upper = permission_type.replace("-", "_").upper()
        if upper in _READ_ONLY_ROLE_PERMISSIONS:
            return {
                "error": (
                    f"{permission_type} is readable but has no write endpoint on "
                    "LibreChat's roles router — it cannot be set through the API. "
                    "Change it in librechat.yaml's interface block instead."
                )
            }
        return {
            "error": (
                f"Unsupported permission_type: {permission_type}. "
                f"Valid values: {', '.join(sorted(_ROLE_PERMISSION_TYPES))}"
            )
        }
    if not permissions:
        return {"error": "permissions is required — provide at least one key to change"}
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        key = _ROLE_PERMISSION_TYPES[permission_type]
        current = await client.request("GET", f"/api/roles/{role}")
        before = ((current or {}).get("permissions") or {}).get(key) or {}

        # Upstream drops an unknown key on a 200, so a typo would report success and
        # change nothing. The role's own current keys are the authoritative set: they
        # are what the server merges into, and reading them needs no second source
        # that could drift from the schema.
        unknown = sorted(set(permissions) - set(before)) if before else []
        if unknown:
            return {
                "error": (
                    f"Unknown permission key(s) for {permission_type}: {', '.join(unknown)}. "
                    f"{role}.{key} has: {', '.join(sorted(before))}. "
                    "LibreChat would accept this with a 200 and change nothing."
                )
            }

        data = await client.request("PUT", f"/api/roles/{role}/{permission_type}", json=permissions)
        result = _as_dict(data, plural="roles")
        after = ((result or {}).get("permissions") or {}).get(key) or {}
        log.warning(
            "set_role_permissions",
            role=role,
            permission_type=permission_type,
            requested=permissions,
            before=before,
            after=after,
            changed=before != after,
        )
        return {
            "role": role,
            "permission_type": permission_type,
            "before": before,
            "after": after,
            "changed": before != after,
            "requested": permissions,
        }
    except Exception as e:
        return _tool_error("set_role_permissions", e)


@mcp.tool
@instrument("ensure_agent")
async def ensure_agent(
    name: str,
    spec: dict,
    mcp_servers: list[str] | None = None,
    create_missing: bool = True,
) -> dict:
    """Idempotently reconcile an agent to a desired spec — create, update or leave alone.

    This is the declarative primitive a fleet rebuild applies: define each agent once
    and re-run this until it stops changing anything.

    Returns `{action, agent_id, changed, agent}` where `action` is `created`,
    `updated` or `unchanged`. **`unchanged` is a real outcome**, reached by diffing
    the current agent against the spec and issuing no write at all — not by PATCHing
    everything and finding it matched.

    That matters beyond tidiness. LibreChat appends to `versions[]` on a real change,
    and `versions[]` is what `revert_agent` walks. A reconciler that wrote
    unconditionally would bury every meaningful revision under identical
    re-applications. (LibreChat does also dedupe a no-op PATCH server-side — measured
    — but relying on that would make correctness here depend on an undocumented
    upstream behaviour, and would still cost a round-trip and report `updated`.)

    Only the fields present in `spec` are compared and sent. An absent field means
    "don't care", not "clear it", which is what lets a partial spec be re-applied
    safely.

    **Name is not unique server-side.** Two agents may share one, so a duplicate is an
    error here rather than a coin flip about which gets rewritten. Erroring loses
    nothing a caller cannot fix; picking one silently can rewrite the wrong agent.

    Args:
        name: The agent's display name, used as the reconciliation key.
        spec: Desired field values, using the same names as create_agent — e.g.
            `{"provider": "Mistral", "model": "mistral-small-latest",
            "instructions": "...", "category": "general", "memory_scope": "agent"}`.
            `provider` and `model` are required when the agent has to be created.
        mcp_servers: MCP servers to attach, e.g. ['searxng']. Expanded to that
            server's tool pluginKeys, because `mcpServerNames` is derived by LibreChat
            and cannot be set. Compared against the agent's derived `mcpServerNames`.
        create_missing: When False, a missing agent is an error rather than a create.
            Use it to reconcile a fleet you expect to already exist.
    """
    if not name:
        return {"error": "name is required"}
    if not isinstance(spec, dict):
        return {"error": "spec must be an object of desired field values"}
    unknown = sorted(set(spec) - set(_AGENT_WRITABLE_FIELDS))
    if unknown:
        return {
            "error": (
                f"Unknown or unsettable spec field(s): {', '.join(unknown)}. "
                f"Settable: {', '.join(_AGENT_WRITABLE_FIELDS)}. "
                "Note isPublic/is_promoted/access_level/mcpServerNames are accepted by "
                "LibreChat and silently ignored — share_agent is the working path."
            )
        }
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY

        desired = dict(spec)
        desired.pop("name", None)  # the key, not a diffable field

        mcp_keys: list[str] = []
        if mcp_servers is not None:
            mcp_keys, unknown_servers = await _expand_mcp_servers(client, mcp_servers)
            if unknown_servers:
                return {
                    "error": (
                        f"Unknown MCP server(s): {', '.join(unknown_servers)}. "
                        "Use list_mcp_servers to see what LibreChat has enumerated."
                    )
                }
            desired["tools"] = [*(spec.get("tools") or []), *mcp_keys]

        # Search then filter exactly, rather than trusting the server-side search to
        # mean equality — it is a fuzzy match, so "Research" would also return
        # "Research Assistant" and the reconciler would key off the wrong agent.
        listing, _, _, _ = await _collect_agents(client, search=name, limit=100, follow_cursor=True)
        matches = [a for a in listing if a.get("name") == name]

        if len(matches) > 1:
            return {
                "error": (
                    f"{len(matches)} agents are named {name!r} "
                    f"({', '.join(str(a.get('id')) for a in matches)}). "
                    "Names are not unique in LibreChat, so this is ambiguous — "
                    "rename or delete the duplicates, or use update_agent by id."
                )
            }

        if not matches:
            if not create_missing:
                return {"error": f"No agent named {name!r} and create_missing is False"}
            created = await create_agent(
                name=name,
                mcp_servers=mcp_servers,
                **spec,  # type: ignore[arg-type]
            )
            if "error" in created:
                return created
            log.info("ensure_agent", name=name, action="created", agent_id=created.get("id"))
            return {
                "action": "created",
                "agent_id": created.get("id"),
                "changed": sorted(desired),
                "agent": created,
            }

        agent_id = matches[0].get("id")
        # The listing is a PROJECTION — no model, provider, tools or instructions — so
        # diffing against it would report "unchanged" for fields it never carried.
        current = await client.request("GET", f"/api/agents/{agent_id}")
        current = _as_dict(current, plural="agents")

        changed = _spec_differences(desired, current)
        # `mcpServerNames` is derived, so it is the field that says whether the
        # attachment actually took. Comparing only `tools` would miss a server whose
        # own tool set changed upstream between two runs of the same spec.
        if (
            mcp_servers is not None
            and sorted(current.get("mcpServerNames") or []) != sorted(mcp_servers)
            and "tools" not in changed
        ):
            changed.append("tools")

        if not changed:
            log.info("ensure_agent", name=name, action="unchanged", agent_id=agent_id)
            return {
                "action": "unchanged",
                "agent_id": agent_id,
                "changed": [],
                "agent": current,
            }

        updated = await update_agent(
            agent_id=agent_id,
            mcp_servers=mcp_servers,
            **{k: v for k, v in spec.items() if k != "name"},  # type: ignore[arg-type]
        )
        if "error" in updated:
            return updated
        log.info("ensure_agent", name=name, action="updated", agent_id=agent_id, changed=changed)
        return {
            "action": "updated",
            "agent_id": agent_id,
            "changed": changed,
            "agent": updated,
        }
    except Exception as e:
        return _tool_error("ensure_agent", e)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@mcp.tool
@instrument("list_models")
async def list_models(enabled_only: bool = True) -> dict:
    """List model ids per provider, for validating `create_agent(model=)`.

    Returns `{"models": {<provider>: [id, ...]}, "providers": [...], "count": N}`.

    **`/api/models` is not a "what is enabled" signal on its own.** Measured on this
    instance: it still lists `anthropic` and its models while that endpoint is fully
    disabled — `/api/endpoints` returns only `['agents', 'Mistral']`. Validating
    against `/api/models` alone would happily accept a model on a dead provider, and
    the failure would surface in chat rather than at call time. So `enabled_only`
    intersects the two, and defaults to on.

    Args:
        enabled_only: Restrict to providers `/api/endpoints` actually offers.
            Set False to see everything `/api/models` reports, disabled included.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        models = await client.request("GET", "/api/models")
        if not isinstance(models, dict):
            return _as_dict(models, plural="models")
        by_provider = {k: v for k, v in models.items() if isinstance(v, list)}
        result: dict[str, Any] = {}
        if enabled_only:
            endpoints = await client.request(
                # text/html with a JSON body upstream — see the flag's docstring.
                "GET",
                "/api/endpoints",
                allow_mislabelled_json=True,
            )
            enabled = set(endpoints) if isinstance(endpoints, dict) else set()
            excluded = sorted(set(by_provider) - enabled)
            by_provider = {k: v for k, v in by_provider.items() if k in enabled}
            if excluded:
                result["excluded_providers"] = excluded
                result["note"] = (
                    "Excluded providers appear in /api/models but are not offered by "
                    "/api/endpoints — they are configured but disabled. Pass "
                    "enabled_only=false to see them."
                )
        result["models"] = by_provider
        result["providers"] = sorted(by_provider)
        result["count"] = sum(len(v) for v in by_provider.values())
        return result
    except Exception as e:
        return _tool_error("list_models", e)


@mcp.tool
@instrument("list_categories")
async def list_categories() -> dict:
    """List the agent-picker categories, with a count of agents in each.

    Returns `{"categories": [{value, label, count, description}], "count": N}`. The
    `value` is what `create_agent(category=)` takes; `label` is an i18n key, not a
    display string.

    Upstream registers this route before `GET /agents/:id`, so `categories` is a
    reserved agent id. Harmless, but an agent literally named `categories` would be
    unreachable by id.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        data = await client.request("GET", "/api/agents/categories")
        return _as_dict(data, plural="categories")
    except Exception as e:
        return _tool_error("list_categories", e)


def _closest(value: str, options: list[str]) -> str:
    """The nearest valid option, for an error that helps rather than just refuses."""
    import difflib

    match = difflib.get_close_matches(value, options, n=1, cutoff=0.6)
    return match[0] if match else ""


@mcp.tool
@instrument("validate_agent_spec")
async def validate_agent_spec(
    provider: str = "",
    model: str = "",
    tools: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    category: str = "",
) -> dict:
    """Check an agent spec against what LibreChat actually offers, before writing it.

    Returns `{"valid": bool, "errors": [...], "warnings": [...], "checked": [...]}`.
    A warning does not make a spec invalid — it flags something that will write
    successfully and then not work, which is the failure mode this API specialises in.
    Every check is
    against a live lookup, so this reflects the running instance rather than a
    remembered configuration.

    Worth running because several of these fail SILENTLY at write time: a typo'd model
    posts happily and fails later in chat, and an unknown tool key is dropped on a 201
    with the agent created without it. `create_agent` reports a dropped tool after the
    fact; this catches it before.

    Errors name the closest valid value where there is one, rather than only saying
    "invalid".

    Args:
        provider: Endpoint name to check, e.g. 'Mistral'.
        model: Model id to check. Requires `provider` to be meaningful.
        tools: Built-in tool pluginKeys to check against list_tools.
        mcp_servers: MCP server names to check against list_mcp_servers.
        category: Category value to check against list_categories.
    """
    try:
        client = get_client()  # inside the try — see _CLIENT_INSIDE_TRY
        errors: list[str] = []
        warnings: list[str] = []
        checked: list[str] = []

        if provider or model:
            checked.append("provider/model")
            models = await client.request("GET", "/api/models")
            endpoints = await client.request(
                # text/html with a JSON body upstream — see the flag's docstring.
                "GET",
                "/api/endpoints",
                allow_mislabelled_json=True,
            )
            enabled = sorted(endpoints) if isinstance(endpoints, dict) else []
            models = models if isinstance(models, dict) else {}
            if provider and provider not in enabled:
                near = _closest(provider, enabled)
                errors.append(
                    f"provider {provider!r} is not offered by this instance"
                    + (f" — did you mean {near!r}?" if near else "")
                    + f" Enabled: {', '.join(enabled) or '(none)'}"
                )
            elif model:
                available = [m for m in (models.get(provider) or []) if isinstance(m, str)]
                if available and model not in available:
                    near = _closest(model, available)
                    errors.append(
                        f"model {model!r} is not offered by {provider!r}"
                        + (f" — did you mean {near!r}?" if near else "")
                    )

        if tools:
            checked.append("tools")
            data = await client.request("GET", "/api/agents/tools")
            builtin = [
                t["pluginKey"] for t in (data or []) if isinstance(t, dict) and t.get("pluginKey")
            ]
            by_server = await _mcp_server_tools(client)
            mcp_keys = {k for keys in by_server.values() for k in keys}
            for t in tools:
                if t in builtin or t in mcp_keys:
                    continue
                near = _closest(t, [*builtin, *sorted(mcp_keys)])
                errors.append(
                    f"tool {t!r} is not available"
                    + (f" — did you mean {near!r}?" if near else "")
                    + ". LibreChat would DROP it silently on a successful create."
                )

        if mcp_servers:
            checked.append("mcp_servers")
            by_server = await _mcp_server_tools(client)
            registry = await client.request("GET", "/api/mcp/servers")
            registry = registry if isinstance(registry, dict) else {}
            for s in mcp_servers:
                if s not in by_server:
                    near = _closest(s, sorted(by_server))
                    errors.append(
                        f"MCP server {s!r} is not configured"
                        + (f" — did you mean {near!r}?" if near else "")
                        + f". Configured: {', '.join(sorted(by_server)) or '(none)'}"
                    )
                elif not by_server[s]:
                    errors.append(
                        f"MCP server {s!r} is configured but enumerates no tools, so "
                        "attaching it would give the agent nothing."
                    )
                # Two independent surfaces, and on this instance they disagree.
                # `/api/mcp/tools` is the CATALOGUE, and it is what attachment
                # resolves against: a jobsearch tool attaches fine and LibreChat
                # derives `mcpServerNames: ['jobsearch']` (measured). `toolFunctions`
                # in `/api/mcp/servers` is the RUNTIME registry, and it is empty for a
                # server whose headers block carries a `{{...}}` placeholder
                # (vikunja#662). So the write succeeds and the agent still receives
                # nothing at chat time — a warning rather than an error, because the
                # spec is valid and it is the runtime that is broken.
                elif not (registry.get(s) or {}).get("toolFunctions"):
                    warnings.append(
                        f"MCP server {s!r} can be attached, but LibreChat's runtime "
                        "registry holds no toolFunctions for it, so the agent will "
                        "receive no tools at chat time. On this instance that is "
                        "vikunja#662 — a placeholder in the server's headers block."
                    )

        if category:
            checked.append("category")
            data = await client.request("GET", "/api/agents/categories")
            values = [c["value"] for c in (data or []) if isinstance(c, dict) and c.get("value")]
            if values and category not in values:
                near = _closest(category, values)
                errors.append(
                    f"category {category!r} is not defined"
                    + (f" — did you mean {near!r}?" if near else "")
                    + f". Valid: {', '.join(values)}"
                )

        return {"valid": not errors, "errors": errors, "warnings": warnings, "checked": checked}
    except Exception as e:
        return _tool_error("validate_agent_spec", e)


# ---------------------------------------------------------------------------
# Liveness and authentication
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def liveness(_request: Request) -> JSONResponse:
    """Liveness probe for the container HEALTHCHECK. Unauthenticated by design.

    It answers for *this process* and deliberately does not touch LibreChat. A
    readiness-style probe that reached upstream would mark this container unhealthy
    whenever LibreChat restarted, and compose would then restart a process that was
    working perfectly — trading a LibreChat blip for a librechat-mcp outage. This
    server is stateless and re-authenticates per request, so it needs no restart to
    recover. If a readiness signal is ever wanted, add a separate `/ready`.

    Unauthenticated means the body is public. It carries a literal status and nothing
    else: no version, no bind address, no `LIBRECHAT_URL`, no account email. This is
    the one route that answers without a token, so anything echoed here is echoed to
    anything that can reach the port.
    """
    return JSONResponse({"status": "ok"})


class _BearerAuthMiddleware:
    """ASGI middleware enforcing static bearer token authentication.

    Requests missing a valid Authorization header receive 401. Non-HTTP scopes
    (lifespan, websocket) pass through unconditionally, as do the paths in
    `_AUTH_EXEMPT_PATHS` — currently `/health` alone, which the container HEALTHCHECK
    calls before it could possibly hold a token.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") not in _AUTH_EXEMPT_PATHS:
            request = Request(scope, receive)
            auth_header = request.headers.get("authorization", "")
            # Sliced by length, NOT `removeprefix("Bearer ")`. The scheme test below
            # is case-insensitive (RFC 7235 §2.1 makes the scheme so), and pairing it
            # with a case-SENSITIVE strip means a conformant `bearer <token>` passes
            # the test and then fails the comparison, because the literal "bearer "
            # is still attached to what gets compared. Rejecting a valid client, for
            # no security gain. Found by test, not by review.
            _SCHEME = "bearer "
            provided = (
                auth_header[len(_SCHEME) :] if auth_header.lower().startswith(_SCHEME) else ""
            )
            # compare_digest, not `==`: a plain comparison short-circuits on the
            # first differing byte and leaks the token's prefix by timing.
            if not hmac.compare_digest(provided, self._token):
                response = Response(
                    content='{"error":"Unauthorized"}',
                    status_code=401,
                    media_type="application/json",
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    configure_logging()
    # Initialise OTEL up front rather than lazily on the first tool call, so a
    # misconfigured endpoint warns at startup instead of on whatever call happens to
    # be first. Returns None and stays silent when the env var is unset.
    if get_tracer() is not None:
        log.info("librechat_mcp_otel_enabled")
    atexit.register(shutdown_observability)

    port = int(os.getenv("MCP_PORT", "8496"))
    api_token = os.environ.get("LIBRECHAT_MCP_API_TOKEN")

    # These two REFUSE rather than warn. A log line is not an access control: nothing
    # sees it unless someone is already tailing startup output, and the process would
    # serve full agent CRUD — including delete_agent — in the meantime.
    if not api_token:
        raise RuntimeError(
            "Refusing to start librechat-mcp without LIBRECHAT_MCP_API_TOKEN set. "
            "This server exposes agent CRUD including delete_agent, and binds 0.0.0.0 "
            "inside its container where anything on the same Docker network can reach "
            "it. Generate a token with: "
            'python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(api_token) < _MIN_API_TOKEN_LENGTH:
        raise RuntimeError(
            f"LIBRECHAT_MCP_API_TOKEN is too short ({len(api_token)} chars, need "
            f">= {_MIN_API_TOKEN_LENGTH}). "
            'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )

    # 0.0.0.0 is required for the server to be reachable from outside its own network
    # namespace, and is NOT the exposure decision — a bind address is a no-op as a
    # security control inside a namespace, and binding the container's own loopback
    # makes it unreachable even through the compose publish (measured: connection
    # refused). The `ports:` publish is the network control; this token is the
    # access control.
    middleware: list[Any] = [Middleware(_BearerAuthMiddleware, token=api_token)]
    log.info("librechat_mcp_starting", port=port, version=__version__)
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, middleware=middleware)


if __name__ == "__main__":  # pragma: no cover
    main()
