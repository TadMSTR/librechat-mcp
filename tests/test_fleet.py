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


# ---------------------------------------------------------------------------
# Pagination — the defects the first live run found
# ---------------------------------------------------------------------------


async def test_pagination_sends_the_cursor_as_cursor_not_after(authed_client):
    """The response names it `after`; the request parameter is `cursor`.

    `v1.js:1663` destructures `const { category, search, limit, cursor, promoted } =
    req.query`, so `after=` is an unknown parameter and is ignored — every page comes
    back as page one, with `has_more: true` and the same `after`, forever. Measured
    both ways on the live instance. This asserts the parameter name, because the whole
    defect is one word.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.get("/api/agents").mock(
            side_effect=[
                httpx.Response(
                    200, json={"data": [{"id": "a1"}], "has_more": True, "after": "CUR1"}
                ),
                httpx.Response(
                    200, json={"data": [{"id": "a2"}], "has_more": False, "after": None}
                ),
            ]
        )
        result = await srv.list_agents(limit=1)

    assert result["count"] == 2, "both pages should be collected"
    second = route.calls[1].request.url
    assert "cursor=CUR1" in str(second)
    assert "after=" not in str(second), "after= is silently ignored upstream"


async def test_pagination_stops_when_the_cursor_does_not_advance(authed_client):
    """The no-progress guard, which is what caught the parameter-name bug live.

    A server that keeps answering `has_more: true` with the same page would otherwise
    be bounded only by the page ceiling — 20 pointless requests before returning a
    wrong answer. Stopping on "no new ids" turns that into one wasted request.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "a1"}], "has_more": True, "after": "STUCK"}
            )
        )
        result = await srv.list_agents(limit=1)

    assert result["count"] == 1, "the repeated agent must not be counted twice"


async def test_pagination_reports_hitting_the_page_ceiling(authed_client):
    """A cap that is not reported is a silent truncation wearing a bound's clothes."""
    with respx.mock(base_url=BASE_URL) as mock:
        pages = [
            httpx.Response(
                200,
                json={"data": [{"id": f"a{i}"}], "has_more": True, "after": f"C{i}"},
            )
            for i in range(srv._MAX_AGENT_PAGES + 2)
        ]
        mock.get("/api/agents").mock(side_effect=pages)
        result = await srv.list_agents(limit=1)

    assert result["count"] == srv._MAX_AGENT_PAGES
    assert result["truncated"] is True
    assert "Narrow the search" in result["warning"]


async def test_list_agents_expand_fetches_full_objects(authed_client):
    """The listing is a projection with no `model`, so a diff against it is worthless."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "One"}]})
        )
        mock.get("/api/agents/a1").mock(
            return_value=httpx.Response(200, json={"id": "a1", "name": "One", "model": "m"})
        )
        result = await srv.list_agents(expand=True)

    assert result["agents"][0]["model"] == "m"
    assert result["expanded"] is True


# ---------------------------------------------------------------------------
# ensure_agent
# ---------------------------------------------------------------------------


async def test_ensure_agent_reports_unchanged_without_writing(authed_client):
    """`unchanged` must mean "no write happened", not "we wrote and it matched".

    LibreChat appends to `versions[]` on a real change and `revert_agent` walks that
    list, so a reconciler that PATCHed unconditionally would bury every meaningful
    revision under identical re-applications.
    """
    current = {
        "id": "a1",
        "name": "Fleet",
        "provider": "Mistral",
        "model": "m",
        "instructions": "do things",
    }
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Fleet"}]})
        )
        mock.get("/api/agents/a1").mock(return_value=httpx.Response(200, json=current))
        patch = mock.patch("/api/agents/a1")
        post = mock.post("/api/agents")
        result = await srv.ensure_agent(
            name="Fleet",
            spec={"provider": "Mistral", "model": "m", "instructions": "do things"},
        )

    assert result["action"] == "unchanged"
    assert result["changed"] == []
    assert not patch.called, "unchanged must issue NO write"
    assert not post.called


async def test_ensure_agent_updates_only_when_a_field_actually_differs(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Fleet"}]})
        )
        mock.get("/api/agents/a1").mock(
            return_value=httpx.Response(
                200, json={"id": "a1", "provider": "Mistral", "model": "m", "instructions": "old"}
            )
        )
        patch = mock.patch("/api/agents/a1").mock(
            return_value=httpx.Response(200, json={"id": "a1", "instructions": "new"})
        )
        result = await srv.ensure_agent(
            name="Fleet", spec={"provider": "Mistral", "model": "m", "instructions": "new"}
        )

    assert result["action"] == "updated"
    assert result["changed"] == ["instructions"]
    assert json.loads(patch.calls[0].request.content) == {
        "provider": "Mistral",
        "model": "m",
        "instructions": "new",
    }


async def test_ensure_agent_ignores_tool_ordering(authed_client):
    """LibreChat returns MCP tool keys in its own resolved order.

    A positional compare would report a difference on every run, so `unchanged` would
    never be reachable and every reconciliation would grow `versions[]`.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Fleet"}]})
        )
        mock.get("/api/agents/a1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "a1",
                    "provider": "Mistral",
                    "model": "m",
                    "tools": ["b_tool", "a_tool"],
                },
            )
        )
        patch = mock.patch("/api/agents/a1")
        result = await srv.ensure_agent(
            name="Fleet",
            spec={"provider": "Mistral", "model": "m", "tools": ["a_tool", "b_tool"]},
        )

    assert result["action"] == "unchanged"
    assert not patch.called


async def test_ensure_agent_refuses_an_ambiguous_name(authed_client):
    """Names are not unique server-side, so picking one silently could rewrite the wrong agent."""
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "a1", "name": "Fleet"}, {"id": "a2", "name": "Fleet"}]},
            )
        )
        patch = mock.patch("/api/agents/a1")
        result = await srv.ensure_agent(name="Fleet", spec={"provider": "Mistral", "model": "m"})

    assert "2 agents are named" in result["error"]
    assert not patch.called


async def test_ensure_agent_matches_the_name_exactly_not_fuzzily(authed_client):
    """`?search=` is a fuzzy match upstream, so 'Research' also returns 'Research Assistant'.

    Keying reconciliation off a near-match would rewrite a different agent than the
    caller named.
    """
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/agents").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "a1", "name": "Research Assistant"},
                        {"id": "a2", "name": "Research"},
                    ]
                },
            )
        )
        mock.get("/api/agents/a2").mock(
            return_value=httpx.Response(200, json={"id": "a2", "provider": "Mistral", "model": "m"})
        )
        result = await srv.ensure_agent(name="Research", spec={"provider": "Mistral", "model": "m"})

    assert result["agent_id"] == "a2"
    assert result["action"] == "unchanged"


async def test_ensure_agent_rejects_a_spec_field_that_is_silently_ignored(authed_client):
    """`isPublic` in a spec would look applied and never be. Refuse it by name."""
    result = await srv.ensure_agent(
        name="Fleet", spec={"provider": "Mistral", "model": "m", "isPublic": True}
    )
    assert "isPublic" in result["error"]
    assert "share_agent" in result["error"]


async def test_ensure_agent_can_refuse_to_create(authed_client):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))
        post = mock.post("/api/agents")
        result = await srv.ensure_agent(
            name="Absent", spec={"provider": "Mistral", "model": "m"}, create_missing=False
        )

    assert "create_missing is False" in result["error"]
    assert not post.called


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_list_models_intersects_with_the_enabled_endpoints(authed_client):
    """`/api/models` alone would accept a model on a disabled provider.

    Measured: it still lists `anthropic` and its models while `/api/endpoints`
    returns only `['agents', 'Mistral']`. The failure would surface in chat, not at
    call time.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/models").mock(
            return_value=httpx.Response(
                200, json={"Mistral": ["mistral-small-latest"], "anthropic": ["claude-x"]}
            )
        )
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200,
                content=b'{"agents": {}, "Mistral": {}}',
                # text/html with a JSON body, exactly as upstream sends it.
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        result = await srv.list_models()

    assert result["providers"] == ["Mistral"]
    assert "anthropic" in result["excluded_providers"]


async def test_endpoints_is_read_despite_its_wrong_content_type(authed_client):
    """`GET /api/endpoints` answers `text/html` with a JSON body on v0.8.8-rc2.

    The client refuses a mislabelled body by default — that strictness is what catches
    LibreChat's 200-with-an-SSE-error. This asserts the narrow opt-in reaches the one
    endpoint that needs it, so a regression shows up as an unreadable provider list
    rather than as silently wrong validation.
    """
    from librechat_mcp.client import get_client

    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200,
                content=b'{"Mistral": {}}',
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        client = get_client()
        strict = await _capture_error(client, "/api/endpoints")
        assert "expected JSON" in strict, "the default must still refuse a mislabelled body"

    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200,
                content=b'{"Mistral": {}}',
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        assert await client.request("GET", "/api/endpoints", allow_mislabelled_json=True) == {
            "Mistral": {}
        }


async def _capture_error(client, path):
    try:
        await client.request("GET", path)
    except Exception as exc:  # the message IS the assertion
        return str(exc)
    return ""


async def test_the_opt_in_still_refuses_an_sse_error_body(authed_client):
    """The relaxation must not open the door the strictness was built to close.

    `_decode_json` tests for SSE first and unconditionally, so LibreChat's
    200-with-`Illegal request` is caught whether or not the caller opted in. Asserting
    it here is what stops the flag quietly becoming a bypass.
    """
    from librechat_mcp.client import LibreChatError, get_client

    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200, content=b'event: error\ndata: {"message":"Illegal request"}\n\n'
            )
        )
        with pytest.raises(LibreChatError, match="Illegal request"):
            await get_client().request("GET", "/api/endpoints", allow_mislabelled_json=True)


async def test_validate_agent_spec_names_the_closest_valid_value(authed_client):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        mock.get("/api/models").mock(return_value=httpx.Response(200, json={"Mistral": ["m1"]}))
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200, content=b'{"Mistral": {}}', headers={"content-type": "text/html"}
            )
        )
        result = await srv.validate_agent_spec(provider="Mistrl")

    assert result["valid"] is False
    assert "did you mean 'Mistral'" in result["errors"][0]


async def test_validate_agent_spec_warns_rather_than_errors_on_a_toolless_registry(
    authed_client,
):
    """A server that attaches fine but delivers nothing at chat time is a WARNING.

    The catalogue and the runtime registry are different surfaces and disagree here:
    a jobsearch tool attaches and LibreChat derives `mcpServerNames: ['jobsearch']`
    (measured live), but its `toolFunctions` are empty. The spec is valid; the runtime
    is broken. Reporting it as invalid would be wrong, and reporting nothing would
    hand the caller an agent that silently does nothing.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        mock.get("/api/mcp/servers").mock(
            return_value=httpx.Response(
                200,
                json={
                    "searxng": {"toolFunctions": {"search_mcp_searxng": {}}},
                    "jobsearch": {},
                },
            )
        )
        result = await srv.validate_agent_spec(mcp_servers=["jobsearch", "searxng"])

    assert result["valid"] is True
    assert len(result["warnings"]) == 1
    assert "jobsearch" in result["warnings"][0]


# ---------------------------------------------------------------------------
# Guard clauses — cheap to write, and each one is a wrong answer if it regresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("get_agent_permissions", {"agent_id": "../etc/passwd"}),
        ("share_agent", {"agent_id": "bad id", "grant": [{"type": "user", "id": "u"}]}),
        ("get_effective_permissions", {"agent_id": "bad id"}),
        ("duplicate_agent", {"agent_id": "../../x"}),
        ("list_agent_versions", {"agent_id": "bad id"}),
        ("revert_agent", {"agent_id": "bad id", "version_index": 0}),
    ],
)
async def test_every_agent_id_taking_tool_validates_it(tool, kwargs, authed_client):
    """The path guard must hold on the new tools too, not just the six it shipped for.

    Each of these interpolates `agent_id` into a URL. A guard that covers most of the
    surface is the kind of gap that only shows up once.
    """
    result = await getattr(srv, tool)(**kwargs)
    assert "Invalid agent_id" in result["error"]


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected"),
    [
        ("get_role_permissions", {"role": ""}, "role is required"),
        (
            "set_role_permissions",
            {"role": "", "permission_type": "agents", "permissions": {"USE": True}},
            "role is required",
        ),
        (
            "set_role_permissions",
            {"role": "USER", "permission_type": "agents", "permissions": {}},
            "permissions is required",
        ),
        (
            "set_role_permissions",
            {"role": "USER", "permission_type": "nonsense", "permissions": {"USE": True}},
            "Unsupported permission_type",
        ),
        ("search_principals", {"query": ""}, "query is required"),
        ("ensure_agent", {"name": "", "spec": {}}, "name is required"),
        ("ensure_agent", {"name": "x", "spec": "not-a-dict"}, "spec must be an object"),
        (
            "revert_agent",
            {"agent_id": "agent_x", "version_index": -1},
            "version_index must be >= 0",
        ),
        (
            "revert_agent",
            {"agent_id": "agent_x", "version_index": True},
            "must be an integer",
        ),
    ],
)
async def test_argument_guards_refuse_before_any_request(tool, kwargs, expected, authed_client):
    """`version_index=True` matters: bool is an int subclass, so a naive check passes it.

    Every case here returns before a request is issued, so respx has nothing mocked —
    a guard that regressed would attempt a real connection and fail loudly.
    """
    result = await getattr(srv, tool)(**kwargs)
    assert expected in result["error"]


async def test_list_mcp_tools_can_be_narrowed_to_one_server(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        result = await srv.list_mcp_tools(server="jobsearch")

    assert set(result["servers"]) == {"jobsearch"}
    assert result["count"] == 1


async def test_validate_agent_spec_checks_tools_and_categories(authed_client):
    """Covers the tools and category branches, and the MCP-key acceptance path.

    An MCP pluginKey must validate against `/api/mcp/tools`, not `/api/agents/tools` —
    otherwise every valid MCP key is reported as invalid.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        mock.get("/api/agents/tools").mock(
            return_value=httpx.Response(200, json=[{"pluginKey": "calculator"}])
        )
        mock.get("/api/agents/categories").mock(
            return_value=httpx.Response(200, json=[{"value": "general"}])
        )
        result = await srv.validate_agent_spec(
            tools=["calculator", "search_mcp_searxng", "nope"], category="generl"
        )

    assert result["valid"] is False
    assert len(result["errors"]) == 2, "the two valid keys must pass, including the MCP one"
    assert any("did you mean 'general'" in e for e in result["errors"])
    assert set(result["checked"]) == {"tools", "category"}


async def test_validate_agent_spec_rejects_a_model_the_provider_does_not_offer(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/models").mock(
            return_value=httpx.Response(200, json={"Mistral": ["mistral-small-latest"]})
        )
        mock.get("/api/endpoints").mock(
            return_value=httpx.Response(
                200, content=b'{"Mistral": {}}', headers={"content-type": "text/html"}
            )
        )
        result = await srv.validate_agent_spec(provider="Mistral", model="mistral-small-lates")

    assert "did you mean 'mistral-small-latest'" in result["errors"][0]


async def test_ensure_agent_refuses_an_unknown_mcp_server_before_listing(authed_client):
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        _mock_mcp_tools(mock)
        listing = mock.get("/api/agents")
        result = await srv.ensure_agent(
            name="Fleet", spec={"provider": "Mistral", "model": "m"}, mcp_servers=["nope"]
        )

    assert "nope" in result["error"]
    assert not listing.called


async def test_ensure_agent_creates_with_the_mcp_expansion(authed_client):
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        mock.get("/api/agents").mock(return_value=httpx.Response(200, json={"data": []}))
        route = mock.post("/api/agents").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "a1",
                    "tools": ["search_mcp_searxng", "fetch_url_mcp_searxng"],
                    "mcpServerNames": ["searxng"],
                },
            )
        )
        result = await srv.ensure_agent(
            name="Fleet", spec={"provider": "Mistral", "model": "m"}, mcp_servers=["searxng"]
        )

    assert result["action"] == "created"
    assert json.loads(route.calls[0].request.content)["name"] == "Fleet"
    assert result["agent"]["mcpServerNames"] == ["searxng"]


async def test_ensure_agent_detects_a_changed_mcp_attachment(authed_client):
    """`mcpServerNames` is the field that says whether the attachment took.

    Comparing only `tools` would miss a server whose own tool set changed upstream
    between two runs of the same spec.
    """
    with respx.mock(base_url=BASE_URL) as mock:
        _mock_mcp_tools(mock)
        mock.get("/api/agents").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "a1", "name": "Fleet"}]})
        )
        mock.get("/api/agents/a1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "a1",
                    "provider": "Mistral",
                    "model": "m",
                    "tools": ["search_mcp_searxng", "fetch_url_mcp_searxng"],
                    "mcpServerNames": [],
                },
            )
        )
        mock.patch("/api/agents/a1").mock(return_value=httpx.Response(200, json={"id": "a1"}))
        result = await srv.ensure_agent(
            name="Fleet", spec={"provider": "Mistral", "model": "m"}, mcp_servers=["searxng"]
        )

    assert result["action"] == "updated"
    assert "tools" in result["changed"]
