"""Bearer authentication on the MCP endpoint.

Until v0.2.0 this server had no authentication at all. It binds `0.0.0.0` in its
container on `librechat-internal`, and an unauthenticated `initialize` was verified
returning 200 from a throwaway container on that network — the same one the LibreChat
app sits on. Full agent CRUD, including `delete_agent`, was reachable by anything
co-resident there.

**On binding loopback instead.** That was the considered alternative and it was
measured, not assumed: a container that binds `127.0.0.1` inside its own namespace is
unreachable through the compose host publish (`docker-proxy` dials the container IP,
not its loopback — verified, connection refused). It would not have hardened the
service, it would have taken it offline. So the token is the access control and the
`ports:` publish is the network control, and neither substitutes for the other.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

import librechat_mcp.server as srv

TOKEN = "a" * 32


def _app() -> Starlette:
    """A minimal ASGI app behind the real middleware.

    Deliberately not the FastMCP app: this exercises the middleware itself, and
    routing every case through the MCP protocol would test the protocol's error
    handling as much as the guard.
    """

    async def ok(_request):
        return JSONResponse({"reached": True})

    app = Starlette(
        routes=[
            Route("/mcp", ok, methods=["GET", "POST"]),
            Route("/health", ok),
            Route("/healthz", ok),
            Route("/health/", ok),
            Route("/health-debug", ok),
        ]
    )
    return srv._BearerAuthMiddleware(app, token=TOKEN)


async def _get(path: str, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


async def test_a_valid_token_is_accepted():
    resp = await _get("/mcp", {"Authorization": f"Bearer {TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"reached": True}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": ""}, id="empty-header"),
        pytest.param({"Authorization": "Bearer "}, id="bearer-with-no-token"),
        pytest.param({"Authorization": f"Bearer {'a' * 31}"}, id="wrong-token-same-shape"),
        pytest.param({"Authorization": f"Bearer  {TOKEN}"}, id="extra-space"),
        pytest.param({"Authorization": TOKEN}, id="token-without-the-scheme"),
        pytest.param({"Authorization": f"Basic {TOKEN}"}, id="wrong-scheme"),
    ],
)
async def test_anything_but_the_exact_token_gets_401(headers):
    resp = await _get("/mcp", headers)
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_the_scheme_is_matched_case_insensitively():
    """RFC 7235 makes the auth scheme case-insensitive, and clients differ."""
    resp = await _get("/mcp", {"Authorization": f"bearer {TOKEN}"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The exemption is a closed list of one, not a namespace
# ---------------------------------------------------------------------------


async def test_health_answers_without_a_token():
    """The container HEALTHCHECK calls this before it could hold a token — and
    baking the token into the HEALTHCHECK would put a credential in every
    `docker inspect`."""
    resp = await _get("/health")
    assert resp.status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/health/", "/health-debug", "/mcp"])
async def test_only_the_exact_health_path_is_exempt(path):
    """A `startswith("/health")` implementation passes the test above and fails here.

    That is the entire reason this test exists: the natural spelling of the exemption
    is a prefix check, and a prefix check silently exempts every future route someone
    adds under that stem.
    """
    resp = await _get(path)
    assert resp.status_code == 401, f"{path} was exempted — the check is too broad"


async def test_the_health_body_echoes_no_configuration():
    """`/health` is the one route that answers without a token, so anything it
    carries is public to whatever can reach the port."""
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/health", "headers": []}
    response = await srv.liveness(Request(scope))
    body = bytes(response.body).decode()

    assert body == '{"status":"ok"}'
    for leak in ("librechat", "3080", "8496", "0.0.0.0", "token", "email", "version"):
        assert leak.lower() not in body.lower()


async def test_non_http_scopes_pass_through():
    """Lifespan and websocket scopes carry no headers; blocking them would break
    application startup rather than secure anything."""
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = srv._BearerAuthMiddleware(app, token=TOKEN)
    await middleware({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


# ---------------------------------------------------------------------------
# Fail closed at startup
# ---------------------------------------------------------------------------


def test_main_refuses_to_start_without_a_token(monkeypatch):
    """A log line is not an access control. Nothing reads it unless someone is
    already tailing startup output, and the process would serve delete_agent in the
    meantime."""
    monkeypatch.delenv("LIBRECHAT_MCP_API_TOKEN", raising=False)
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: pytest.fail("started with no token"))

    with pytest.raises(RuntimeError, match="without LIBRECHAT_MCP_API_TOKEN"):
        srv.main()


def test_main_refuses_a_token_under_the_minimum_length(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_MCP_API_TOKEN", "tooshort")
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: pytest.fail("started with a short token"))

    with pytest.raises(RuntimeError, match="too short"):
        srv.main()


def test_main_installs_the_middleware_with_the_configured_token(monkeypatch):
    """Requiring a token is not the same as verifying one. This asserts the token
    actually reaches the middleware that enforces it, rather than being read,
    length-checked and then dropped."""
    monkeypatch.setenv("LIBRECHAT_MCP_API_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_PORT", "8496")
    captured: dict = {}
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: captured.update(kw))

    srv.main()

    assert captured["transport"] == "streamable-http"
    assert captured["port"] == 8496
    # 0.0.0.0 is required inside a network namespace — see the comment in main().
    assert captured["host"] == "0.0.0.0"

    (middleware,) = captured["middleware"]
    assert middleware.cls is srv._BearerAuthMiddleware
    assert middleware.kwargs["token"] == TOKEN


def test_main_honours_a_custom_port(monkeypatch):
    monkeypatch.setenv("LIBRECHAT_MCP_API_TOKEN", TOKEN)
    monkeypatch.setenv("MCP_PORT", "9999")
    captured: dict = {}
    monkeypatch.setattr(srv.mcp, "run", lambda **kw: captured.update(kw))

    srv.main()
    assert captured["port"] == 9999
