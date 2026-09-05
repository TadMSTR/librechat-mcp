"""Fleet-provisioning behaviour: MCP attachment, field coverage, silent-drop guards.

Every assertion here corresponds to something measured against the live v0.8.8-rc2
instance during the part 4 probe, not to something read off a schema. Where the two
disagreed, the live instance won — most consequentially over `mcpServerNames`, which
the build plan specified as a settable field and which is in fact derived.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import librechat_mcp.server as srv

from .conftest import BASE_URL

pytestmark = pytest.mark.anyio

MCP_TOOLS_BODY = {
    "servers": {
        "searxng": {
            "tools": [
                {"name": "search", "pluginKey": "search_mcp_searxng"},
                {"name": "fetch_url", "pluginKey": "fetch_url_mcp_searxng"},
            ]
        },
        "jobsearch": {"tools": [{"name": "search_jobs", "pluginKey": "search_jobs_mcp_jobsearch"}]},
    }
}


def _mock_mcp_tools(mock):
    mock.get("/api/mcp/tools").mock(return_value=httpx.Response(200, json=MCP_TOOLS_BODY))


# ---------------------------------------------------------------------------
# _dropped_tools — the guard over LibreChat's silent tool drop
# ---------------------------------------------------------------------------


def test_dropped_tools_reports_what_did_not_survive():
    """Measured live: posting an unknown tool returns 201 with it absent from `tools`.

    `filterAuthorizedTools` removes unknown and unauthorised keys and the request
    still succeeds, so the agent is created without the capability it was created
    for and nothing in the response says so.
    """
    returned = {"tools": ["calculator"]}
    assert _dropped(["calculator", "not_a_real_tool"], returned) == ["not_a_real_tool"]
    assert _dropped(["calculator"], returned) == []
    assert _dropped(None, returned) == []
    assert _dropped([], returned) == []


def _dropped(requested, returned):
    return srv._dropped_tools(requested, returned)


def test_dropped_tools_stays_quiet_when_the_response_shape_is_unexpected():
    """No `tools` key, or a non-list one, must not be read as "everything was dropped".

    A false alarm on every call would train the caller to ignore the field, which
    would cost the real signal.
    """
    assert _dropped(["calculator"], {}) == []
    assert _dropped(["calculator"], {"tools": "not-a-list"}) == []
    assert _dropped(["calculator"], ["not", "a", "dict"]) == []


# ---------------------------------------------------------------------------
# MCP attachment — mcpServerNames is DERIVED, so we go through `tools`
# ---------------------------------------------------------------------------


async def test_mcp_servers_expands_to_that_servers_tool_keys(authed_client):
    """`mcp_servers=['searxng']` must post searxng's pluginKeys in `tools`.

    Posting `mcpServerNames` instead is silently ignored — verified live:
    `{'mcpServerNames': ['searxng']}` with no MCP tool returns `mcpServerNames: []`.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "agent_x",
                    "tools": ["search_mcp_searxng", "fetch_url_mcp_searxng"],
                    "mcpServerNames": ["searxng"],
                },
            )
        )
        result = await srv.create_agent(
            provider="Mistral", model="mistral-small-latest", mcp_servers=["searxng"]
        )

    posted = json.loads(route.calls[0].request.content)
    assert posted["tools"] == ["search_mcp_searxng", "fetch_url_mcp_searxng"]
    assert "mcpServerNames" not in posted, "posting mcpServerNames is a no-op — do not send it"
    assert result["mcpServerNames"] == ["searxng"]
    assert "dropped_tools" not in result


async def test_mcp_servers_appends_to_explicit_tools(authed_client):
    """Both arguments together must union, not overwrite each other."""
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(201, json={"id": "agent_x", "tools": []})
        )
        await srv.create_agent(
            provider="Mistral",
            model="m",
            tools=["calculator"],
            mcp_servers=["searxng"],
        )

    posted = json.loads(route.calls[0].request.content)
    assert posted["tools"] == [
        "calculator",
        "search_mcp_searxng",
        "fetch_url_mcp_searxng",
    ]


async def test_unknown_mcp_server_is_refused_before_the_write(authed_client):
    """Refuse rather than post keys LibreChat would drop on a 201.

    Naming the server the caller got wrong is the whole value: the alternative is a
    created agent that silently lacks the capability it was created for.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mock_mcp_tools(mock)
        create = mock.post("/api/agents")
        result = await srv.create_agent(provider="Mistral", model="m", mcp_servers=["nosuchserver"])

    assert "nosuchserver" in result["error"]
    assert not create.called, "must not write when a requested server does not exist"


async def test_update_with_empty_mcp_servers_detaches(authed_client):
    """`mcp_servers=[]` clears the tool list, which is how detaching works.

    `[]` and `None` must differ: `None` means "leave tools alone", `[]` means
    "attach no MCP server". Verified live — the detach cleared both `tools` and the
    derived `mcpServerNames`.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        route = mock.patch("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x", "tools": []})
        )
        await srv.update_agent(agent_id="agent_x", mcp_servers=[])

    assert json.loads(route.calls[0].request.content) == {"tools": []}


async def test_update_without_mcp_servers_does_not_touch_tools(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.patch("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x"})
        )
        await srv.update_agent(agent_id="agent_x", name="renamed")

    assert json.loads(route.calls[0].request.content) == {"name": "renamed"}


# ---------------------------------------------------------------------------
# Field coverage
# ---------------------------------------------------------------------------


async def test_create_sends_every_writable_field_it_is_given(authed_client):
    """The fields proven to round-trip live, asserted as actually reaching the wire."""
    spec = {
        "category": "it",
        "memory_scope": "agent",
        "artifacts": "default",
        "recursion_limit": 7,
        "end_after_tools": True,
        "hide_sequential_outputs": True,
        "support_contact": {"name": "Ted", "email": "x@example.com"},
        "skills_scope": "selected",
        "skills_enabled": False,
        "skill_authoring_enabled": False,
        "stateful_code_sessions": False,
        "tool_options": {"calculator": {"defer_loading": True}},
        "tool_resources": {"execute_code": {"file_ids": []}},
        "agent_ids": [],
        "edges": [],
    }
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(201, json={"id": "agent_x"})
        )
        await srv.create_agent(provider="Mistral", model="m", **spec)

    posted = json.loads(route.calls[0].request.content)
    for key, value in spec.items():
        assert posted[key] == value, f"{key} did not reach the request body"


async def test_falsy_field_values_are_not_dropped(authed_client):
    """`False` and `0` are meaningful settings, so the filter must test `is not None`.

    A truthiness filter here would make `end_after_tools=False` unsettable while
    reporting success — the same silent-drop shape this part exists to close.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.patch("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x"})
        )
        await srv.update_agent(
            agent_id="agent_x", end_after_tools=False, recursion_limit=0, skills_enabled=False
        )

    posted = json.loads(route.calls[0].request.content)
    assert posted == {"end_after_tools": False, "recursion_limit": 0, "skills_enabled": False}


async def test_create_surfaces_a_silent_tool_drop(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/agents").mock(
            return_value=httpx.Response(201, json={"id": "agent_x", "tools": ["calculator"]})
        )
        result = await srv.create_agent(
            provider="Mistral", model="m", tools=["calculator", "bogus"]
        )

    assert result["dropped_tools"] == ["bogus"]
    assert "silently dropped" in result["warning"]


async def test_update_with_no_fields_still_refuses(authed_client):
    assert "No fields to update" in (await srv.update_agent(agent_id="agent_x"))["error"]


# ---------------------------------------------------------------------------
# MCP discovery
# ---------------------------------------------------------------------------


async def test_list_mcp_tools_reads_the_mcp_endpoint_not_the_agent_one(authed_client):
    """`/api/agents/tools` lists 13 built-ins and NO MCP tool — measured on rc2.

    Validating an MCP pluginKey against it would reject every valid one, so the two
    surfaces must stay separate.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mock_mcp_tools(mock)
        agents_tools = mock.get("/api/agents/tools")
        result = await srv.list_mcp_tools()

    assert not agents_tools.called
    assert result["servers"]["searxng"] == ["search_mcp_searxng", "fetch_url_mcp_searxng"]
    assert result["count"] == 3


async def test_list_mcp_tools_rejects_an_unknown_server_by_name(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        result = await srv.list_mcp_tools(server="nope")

    assert "nope" in result["error"]
    assert "searxng" in result["error"], "the error should name the valid values"


async def test_list_mcp_servers_reports_tool_count_beside_connection_state(authed_client):
    """`disconnected` alone is not a fault — `tool_count` is what says it is usable.

    Measured live: `jobsearch` reads `disconnected` while enumerating 18 tools, and
    `searxng` reads `connected` with 7. Reporting only the state would misdescribe
    both.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        mock.get("/api/mcp/servers").mock(
            return_value=httpx.Response(
                200,
                json={
                    "searxng": {"type": "streamable-http", "source": "yaml"},
                    "jobsearch": {"type": "streamable-http", "source": "yaml"},
                },
            )
        )
        mock.get("/api/mcp/connection/status").mock(
            return_value=httpx.Response(
                200,
                json={
                    "connectionStatus": {
                        "searxng": {"connectionState": "connected"},
                        "jobsearch": {"connectionState": "disconnected"},
                    }
                },
            )
        )
        result = await srv.list_mcp_servers()

    by_name = {s["name"]: s for s in result["servers"]}
    assert by_name["searxng"]["tool_count"] == 2
    assert by_name["jobsearch"]["connectionState"] == "disconnected"
    assert by_name["jobsearch"]["tool_count"] == 1


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_writable_field_list_matches_the_tool_signatures():
    """`_AGENT_WRITABLE_FIELDS` must describe what create/update actually send.

    It documents what LibreChat's zod schemas accept, and a list that drifts from the
    signatures is worse than none — it would assert coverage the tools do not have.
    Binding it to the real signatures makes a field added to one and not the other a
    test failure rather than a comment that quietly went stale.
    """
    import inspect

    create = set(inspect.signature(srv.create_agent).parameters)
    update = set(inspect.signature(srv.update_agent).parameters)
    documented = set(srv._AGENT_WRITABLE_FIELDS)

    # `mcp_servers` is this server's own argument, not a LibreChat field — it expands
    # into `tools`, which IS in the list.
    assert documented <= create | {"mcp_servers"}
    assert documented - create == set(), (
        f"documented but not on create_agent: {documented - create}"
    )
    assert documented - update == set(), (
        f"documented but not on update_agent: {documented - update}"
    )
    for dropped in ("isPublic", "is_promoted", "access_level", "tool_kwargs", "mcpServerNames"):
        assert dropped not in create, f"{dropped} is silently ignored upstream — do not offer it"


async def test_acl_tools_resolve_the_agent_id_to_the_mongo_id(authed_client):
    """The ACL routes are keyed by `_id`, and the public id gets a 200 with nothing.

    Measured on v0.8.8-rc2: `GET /api/permissions/agent/<agent_id>` returns
    `{principals: [], public: false}` for an agent that has two principals. Only the
    PUT rejects the wrong key. So a tool that passed the id through would tell its
    caller "nobody has access" about an agent they own — a confident wrong answer, not
    an error. This asserts the resolution actually happens.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x", "_id": "MONGO_ID"})
        )
        route = mock.get("/api/permissions/agent/MONGO_ID").mock(
            return_value=httpx.Response(
                200,
                json={
                    "resourceType": "agent",
                    "resourceId": "MONGO_ID",
                    "principals": [{"type": "user", "id": "u1", "accessRoleId": "agent_viewer"}],
                    "public": False,
                },
            )
        )
        result = await srv.get_agent_permissions(agent_id="agent_x")

    assert route.called, "must address the ACL route by _id, not by the public agent id"
    assert result["principals"][0]["accessRoleId"] == "agent_viewer"
    assert result["agent_id"] == "agent_x", "the caller's own id should come back to them"


async def test_share_agent_posts_access_role_ids_in_updated_and_removed(authed_client):
    """Grants are named by `accessRoleId`; `permBits` is internal and is not posted.

    `updated` and `removed` are separate arrays server-side, so omitting a principal
    does not revoke it — revocation has to be its own argument.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x", "_id": "MONGO_ID"})
        )
        route = mock.put("/api/permissions/agent/MONGO_ID").mock(
            return_value=httpx.Response(200, json={"message": "Permissions updated successfully"})
        )
        await srv.share_agent(
            agent_id="agent_x",
            grant=[{"type": "user", "id": "u1", "accessRoleId": "agent_viewer"}],
            revoke=[{"type": "user", "id": "u2"}],
        )

    posted = json.loads(route.calls[0].request.content)
    assert posted["updated"] == [{"type": "user", "id": "u1", "accessRoleId": "agent_viewer"}]
    assert posted["removed"] == [{"type": "user", "id": "u2"}]
    assert "permBits" not in route.calls[0].request.content.decode()
    assert "publicAccessRoleId" not in posted, "public must stay untouched unless asked for"


async def test_share_agent_only_sends_public_when_asked(authed_client):
    """`public_access_role_id` defaults to None = leave alone, never to a False-y set.

    Sharing publicly discloses the agent's instructions, files and tools, so changing
    it must be an explicit act rather than a side effect of any other grant.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x", "_id": "MONGO_ID"})
        )
        route = mock.put("/api/permissions/agent/MONGO_ID").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        await srv.share_agent(agent_id="agent_x", public_access_role_id="agent_viewer")

    assert json.loads(route.calls[0].request.content)["publicAccessRoleId"] == "agent_viewer"


async def test_share_agent_refuses_a_no_op(authed_client):
    result = await srv.share_agent(agent_id="agent_x")
    assert "Nothing to do" in result["error"]


async def test_share_agent_flags_the_memory_partition_on_revoke(authed_client):
    """Revoking can strand that user's memories for the agent — say so, do not hide it."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents/agent_x").mock(
            return_value=httpx.Response(200, json={"id": "agent_x", "_id": "MONGO_ID"})
        )
        mock.put("/api/permissions/agent/MONGO_ID").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        result = await srv.share_agent(agent_id="agent_x", revoke=[{"type": "user", "id": "u2"}])

    assert "memory partition" in result["note"]


async def test_get_resource_access_roles_rejects_a_type_outside_the_closed_set(authed_client):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/api/permissions/nonsense/roles")
        result = await srv.get_resource_access_roles(resource_type="nonsense")

    assert not route.called
    assert "promptGroup" in result["error"], "the error should name the valid values"


async def test_get_resource_access_roles_wraps_the_array(authed_client):
    """This endpoint returns a bare array — the #672 shape, in a brand-new tool."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/permissions/agent/roles").mock(
            return_value=httpx.Response(200, json=[{"accessRoleId": "agent_viewer", "permBits": 1}])
        )
        result = await srv.get_resource_access_roles()

    assert result == {"roles": [{"accessRoleId": "agent_viewer", "permBits": 1}], "count": 1}


async def test_set_role_permissions_refuses_an_unknown_key_upstream_would_swallow(authed_client):
    """Measured: `{"NOT_A_REAL_PERMISSION": true}` returns 200 and changes nothing.

    Reporting that as applied is the failure this whole part exists to stop, so the
    key set is checked against what the role actually carries before writing.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/roles/USER").mock(
            return_value=httpx.Response(
                200, json={"permissions": {"AGENTS": {"USE": True, "SHARE": False}}}
            )
        )
        write = mock.put("/api/roles/USER/agents")
        result = await srv.set_role_permissions(
            role="USER", permission_type="agents", permissions={"NOT_A_REAL_PERMISSION": True}
        )

    assert not write.called, "must not write a key upstream would silently drop"
    assert "NOT_A_REAL_PERMISSION" in result["error"]
    assert "SHARE" in result["error"], "the error should name the keys that do exist"


async def test_set_role_permissions_reports_before_and_after(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/roles/USER").mock(
            return_value=httpx.Response(
                200, json={"permissions": {"AGENTS": {"USE": True, "SHARE": False}}}
            )
        )
        route = mock.put("/api/roles/USER/agents").mock(
            return_value=httpx.Response(
                200, json={"permissions": {"AGENTS": {"USE": True, "SHARE": True}}}
            )
        )
        result = await srv.set_role_permissions(
            role="USER", permission_type="agents", permissions={"SHARE": True}
        )

    assert json.loads(route.calls[0].request.content) == {"SHARE": True}, (
        "partial body, server merges"
    )
    assert result["before"] == {"USE": True, "SHARE": False}
    assert result["after"] == {"USE": True, "SHARE": True}
    assert result["changed"] is True


async def test_set_role_permissions_refuses_a_read_only_permission(authed_client):
    """WEB_SEARCH and friends appear in the GET but have no write endpoint.

    A tool that pretended to set them would 404 at best and mislead at worst.
    """
    result = await srv.set_role_permissions(
        role="USER", permission_type="web-search", permissions={"USE": False}
    )
    assert "no write endpoint" in result["error"]


async def test_get_effective_permissions_uses_the_all_route_without_an_agent(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/permissions/agent/effective/all").mock(
            return_value=httpx.Response(200, json={"MONGO_A": 15, "MONGO_B": 1})
        )
        result = await srv.get_effective_permissions()

    assert result == {"permissions": {"MONGO_A": 15, "MONGO_B": 1}, "count": 2}
