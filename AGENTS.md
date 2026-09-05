# librechat-mcp

FastMCP server wrapping the LibreChat REST API. Gives forge agents programmatic CRUD
access to LibreChat agents, handling JWT authentication transparently.

## What it does

Provides 6 tools for listing, reading, creating, updating, and deleting LibreChat agents,
plus a capability lookup used when composing new agents.

## Tools

22, listed with their parameters in README.md. Agents: `list_agents`, `get_agent`,
`create_agent`, `update_agent`, `delete_agent`, `ensure_agent`, `duplicate_agent`,
`list_agent_versions`, `revert_agent`. Access control: `get_agent_permissions`,
`share_agent`, `search_principals`, `get_resource_access_roles`,
`get_effective_permissions`, `get_role_permissions`, `set_role_permissions`.
Discovery: `list_tools`, `list_mcp_tools`, `list_mcp_servers`, `list_models`,
`list_categories`, `validate_agent_spec`.

`agent_id` must match `^[a-zA-Z0-9_-]+$`. IDs come from `list_agents` or `create_agent`.

### Every tool returns a dict — route returns through `_as_dict`

FastMCP builds a tool's output schema from its return annotation and refuses to
serialise a mismatch, so a `-> dict` tool returning a bare list fails **on a successful
upstream response**. That was vikunja#672: `list_tools` was 100% dead through MCP for
all of v0.2.0 while its test stayed green, because the test called the undecorated
function and never crossed the boundary.

Wrap list results as `{"<plural>": [...], "count": N}`. Four LibreChat endpoints return
bare arrays. **A new tool must be added to `TOOL_ARGUMENTS` in
`tests/test_mcp_layer.py`** — that module asserts the exercised set equals the
registered set, so an omission fails the suite rather than quietly shrinking coverage.

### What LibreChat accepts and then ignores

The Mongoose schema is a superset and is **not** the contract. `agentCreateSchema` and
`agentUpdateSchema` (`packages/api/src/agents/validation.ts`) are zod in `'strip'`
mode: an undeclared key is dropped silently and the request still returns 200/201.
Before wrapping any field, round-trip it against a live instance — a `PATCH` 200 proves
nothing.

Measured on v0.8.8-rc2 and deliberately not exposed:

- `mcpServerNames` — **derived** from `tools` in `v1.js` (create :880, update :1304),
  never read from the body. Attach via `mcp_servers=`, which expands to pluginKeys.
- `isPublic`, `is_promoted`, `access_level`, `tool_kwargs` — accepted, stripped.

Also silent: an unknown tool key is dropped by `filterAuthorizedTools` on a 201, so the
agent is created without the capability. `_dropped_tools` surfaces it.

### Two id spaces

Everything under `/api/agents/*` speaks the public `agent_...` id. Everything under
`/api/permissions/*` is keyed by the Mongo `_id`, and the GETs return an empty or zero
result for the wrong one rather than erroring. Resolve with `_resolve_resource_id`;
never expose `_id` to callers.

## Structure

```
librechat_mcp/
  __init__.py   Package marker, __version__
  server.py        FastMCP setup, tool handlers, bearer middleware, main()
  client.py        LibreChatClient — async httpx wrapper, JWT login + refresh
  observability.py structlog config + opt-in OTEL (adapted from nats-mcp v0.3.0)
tests/             pytest tests
deploy/            compose fragment for the librechat stack — NOT a runnable stack
pyproject.toml
```

## Dependencies

| Package   | Role                    |
|-----------|-------------------------|
| fastmcp   | MCP server framework    |
| httpx     | Async HTTP client       |
| structlog | JSON structured logging |
| starlette | ASGI primitives for the bearer middleware (arrives via fastmcp) |

`fastmcp` is pinned to `>=3.2.0,<4.0.0`. It was `>=2.0`, and because
`docker-publish.yml` rebuilds `:latest` on every push to `main`, an open-ended range
means the merge that lands a fix also ships an untested major. Widen it deliberately,
with the suite run against the new major.

## Configuration

| Env var                    | Required | Default  | Purpose                                   |
|----------------------------|----------|----------|-------------------------------------------|
| `LIBRECHAT_URL`            | Yes      | —        | LibreChat base URL                        |
| `LIBRECHAT_ADMIN_EMAIL`    | Yes      | —        | Admin account email for JWT login         |
| `LIBRECHAT_ADMIN_PASSWORD` | Yes      | —        | Admin account password for JWT login      |
| `MCP_PORT`                 | No       | `8496`   | Port to bind (streamable-http transport)  |
| `LIBRECHAT_MCP_API_TOKEN`  | **Yes**  | —        | Bearer token, min 16 chars. Refuses to start without it |
| `LOG_LEVEL`                | No       | `INFO`   | Logging verbosity                         |
| `LOG_FILE`                 | No       | unset    | Optional file sink; leave unset in a container |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No    | unset    | Enables OTEL. gRPC **4317**, not 4318     |

## Key architecture decisions

- **The User-Agent must parse to a browser NAME.** LibreChat's `uaParser` middleware runs
  `ua-parser-js` over the header and rejects the request when `ua.browser.name` is falsy.
  A UA merely existing is not enough — `Mozilla/5.0 (compatible; librechat-mcp/0.1.0)`
  parses to `{}` and is rejected exactly like no header at all. This is not evasion: the
  guard applies to every client of `/api/agents/*` and there is no exemption to request,
  so the UA carries `Chrome/131.0.0.0` alongside an honest `librechat-mcp/<version>` token.
  **Do not "clean up" that string.** `tests/test_user_agent.py` applies the real rule with
  a real parser, and carries the negative control that makes it meaningful.

- **A 200 from this API is not proof of success.** LibreChat returns some rejections as
  HTTP 200 with an SSE body and *no content-type header at all*. Every response goes
  through `_decode_json`, which refuses anything that is not JSON and treats a MISSING
  content-type as failure. Any new content-type check must do the same — an absent header
  must not satisfy it.

- **No tool may raise.** Every handler catches `Exception`, not a named pair, and
  `get_client()` is called INSIDE the try. An exception escaping a tool reaches the MCP
  layer as a raw traceback with nothing logged, which is how #657 stayed invisible for six
  weeks.

- **Transparent JWT handling** — the client logs in via `POST /api/auth/login`, caches the
  JWT in-process, and refreshes 60 seconds before the `exp` claim **in the token itself**,
  with a 401 re-login as a backstop. The measured lifetime is **900 seconds**; the code
  reads `exp` rather than trusting that number, because the previous constant said 6 days
  against a documented 7 and neither was true. Login is rate-limited (~4 per 5 minutes), so
  concurrent callers are collapsed into a single login under a lock.

- **Fail closed on auth.** The MCP endpoint requires a bearer token and `main()` refuses to
  start without one of at least 16 characters. `/health` is exempt by EXACT path — never a
  prefix test, which would also exempt `/healthz` and anything later added under that stem.

- **Full CRUD surface** — unlike read-only forge MCP servers, this one exposes create/update/
  delete because managing agents is its whole purpose. `agent_id` validation guards the path.

## Testing

```bash
pip install -e ".[dev,otel]"
pytest
```

`.[dev,otel]`, not `.[dev]`. The OTEL tests deliberately do not use `importorskip` — with
a dev-only install they would skip themselves into a silent green, which is the failure
the extra exists to prevent. CI installs the same.

Coverage is gated at `fail_under = 90`, with `observability.py` INSIDE the gate.

**A green suite is not a working server.** The suite mocks LibreChat, and a mocked
LibreChat accepts whatever User-Agent you send it — which is exactly how a wrong one
shipped twice. `tests/test_mcp_layer.py` closes the serialisation half of that gap, but
not the semantic half: the three defects found during part 4 — the `after`/`cursor`
parameter name, `/api/endpoints` answering `text/html`, and the catalogue disagreeing
with the runtime registry — were all found by running against the real instance and
none were reachable from a mock. Before closing anything, call a tool against the
running container:

```bash
docker compose pull && docker compose up -d librechat-mcp   # NOT restart
```

`docker-publish.yml` republishes `:latest` on every push to `main` but redeploys nothing,
and `restart` will not pick up a new image. That gap is where a "fixed" service stays
broken.

## Git workflow

Branch before editing — do not commit directly to `main`.
