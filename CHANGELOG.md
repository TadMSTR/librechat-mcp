# Changelog

## [0.3.0] — 2026-09-05

Fleet management: `librechat-mcp` goes from 6 agent-CRUD tools to 22, covering MCP
attachment, the RBAC surface, idempotent provisioning and call-time validation.

Verified against **LibreChat `v0.8.8-rc2`** (image
`ghcr.io/danny-avila/librechat:v0.8.8-rc2`), every tool exercised through the MCP layer
against that running instance rather than by calling the Python functions.

### Fixed

- **`list_tools` was 100% dead through MCP and is now the convention.** It was annotated
  `-> dict` and returned the bare JSON array LibreChat answers `/api/agents/tools` with,
  so FastMCP refused it with `structured_content must be a dict or None` — *on a
  successful upstream response*. Its unit test passed the whole time because it called
  the undecorated function object and never crossed the boundary that breaks. Fixed with
  an `_as_dict()` helper on every return path, so a `-> dict` tool cannot emit a list, a
  scalar or anything else whatever upstream sends. (vikunja#672)
- **Pagination sent the wrong parameter name.** The response envelope reports the cursor
  as `after`; the handler reads `req.query.cursor`. An unknown query parameter is
  ignored, so every page came back as page one with `has_more: true` and an identical
  cursor — an infinite loop for any client that trusted it. The `last_id` fallback is
  gone too: it is an agent id, not a cursor, and passing it re-returns the same page.
- **`list_agents` no longer truncates silently.** It follows the cursor by default, with
  two independent stops — a page ceiling and a no-progress guard — and reports hitting
  either as `truncated` plus a warning.

### Added

- **`ensure_agent(name, spec)`** — idempotent create-or-update by name, returning
  `created`, `updated` or `unchanged`. `unchanged` means no write was issued: the
  current agent is fetched and diffed first, so reconciliation does not append to
  `versions[]` and bury the revert history. Names are matched exactly (`?search=` is
  fuzzy upstream) and a duplicate name is an error rather than a coin flip.
- **RBAC**: `get_agent_permissions`, `share_agent`, `search_principals`,
  `get_resource_access_roles`, `get_effective_permissions`, `get_role_permissions`,
  `set_role_permissions`.
- **Agent routes**: `duplicate_agent`, `revert_agent`, `list_agent_versions`, and
  `get_agent(expanded=)`.
- **Discovery and validation**: `list_mcp_tools`, `list_mcp_servers`, `list_models`,
  `list_categories`, `validate_agent_spec`.
- **Field coverage on create/update goes 8 → 28**, each round-tripped individually
  against the live instance — including `memory_scope` (`'user'|'agent'`, per-agent
  memory isolation), `category`, `edges`, `subagents`, `tool_options`, `tool_resources`
  and `support_contact`.
- **`create_agent(mcp_servers=['searxng'])`** attaches an MCP server by expanding it to
  that server's tool pluginKeys.
- **`tests/test_mcp_layer.py`** — a gate invoking every registered tool through the MCP
  layer against upstream bodies that break serialisation, asserting the exercised set
  equals the registered set. A tool added without a case fails the gate rather than
  being silently skipped. Needs no live instance.

### Changed

- `LibreChatClient.request()` gains `allow_mislabelled_json`, used at exactly the two
  `/api/endpoints` call sites: that endpoint answers `text/html; charset=utf-8` with a
  JSON body. The SSE-error check runs first and unconditionally, so the relaxation
  cannot pass an `Illegal request` — asserted by a test.
- `list_models` intersects `/api/models` with `/api/endpoints`, because the former still
  lists `anthropic` and its models with that endpoint fully disabled.

### Notes on LibreChat behaviour these tools work around

All measured on `v0.8.8-rc2`, all cases where the API succeeds and does not do what was
asked:

- **`mcpServerNames` cannot be set.** Absent from the zod create/update schemas and
  derived server-side from `tools` entries carrying the `_mcp_` delimiter. Posting it
  returns 201 and changes nothing.
- **Unknown tools are dropped silently on a 201** by `filterAuthorizedTools`, so the
  agent is created without the capability. Reported as `dropped_tools` + `warning`.
- **`isPublic`, `is_promoted`, `access_level` and `tool_kwargs` are accepted and
  ignored** — zod `'strip'` mode. Not exposed as parameters; use `share_agent`.
- **The `/api/permissions/*` routes are keyed by the Mongo `_id`.** With the public
  agent id, the GET returns `200 {principals: [], public: false}` and `/effective`
  returns `{permissionBits: 0}` — wrong answers, not errors. Only the PUT rejects it.
  These tools take the public id and resolve `_id` internally.
- **An unknown role permission key returns 200 and changes nothing**, so
  `set_role_permissions` validates keys against what the role carries before writing.

## [0.2.0] — 2026-09-05

Every tool in this server had failed in production since it shipped. This release fixes
that, and adds the things whose absence let it go unnoticed for six weeks.

### Fixed

- **The User-Agent.** LibreChat's `uaParser` middleware runs `ua-parser-js` over the
  header and rejects any request whose `ua.browser.name` is falsy. The UA added by
  `129fc66` to fix #53-#56 — `Mozilla/5.0 (compatible; librechat-mcp/0.1.0)` — parses to
  `{}` and is rejected exactly like sending no header at all. It now carries
  `Chrome/131.0.0.0` alongside an honest `librechat-mcp/<version>` token, interpolated
  from `__version__`. (vikunja#657; supersedes #53, #54, #55, #56)
- **A 200 is no longer treated as success.** LibreChat returns that rejection as HTTP 200
  with an SSE body and *no content-type header*, so `_raise_for_status` passed and
  `resp.json()` raised `JSONDecodeError` — a class in no `except` clause on the path. It
  escaped as a raw traceback: nothing logged, nothing measured. Responses now go through
  `_decode_json`, which unwraps the SSE error and treats a **missing** content-type as
  failure rather than as a pass.
- **No tool can return a raw traceback.** Handlers broadened from a named pair to
  `Exception`, and `get_client()` moved inside the `try` — it raises
  `LibreChatConfigError`, the very class the old clause named, from outside the block that
  named it, so that guard could never fire.
- **JWT lifetime.** Measured at **900 seconds**, not the 7 days every doc claimed; the
  constant refreshed after 6 days, so the proactive refresh had never once fired. Refresh
  is now driven by the `exp` claim in the token itself, 60s ahead of expiry.
- **Login stampedes.** `POST /api/auth/login` is rate-limited at ~4 attempts per 5 minutes.
  Concurrent callers on an expired token are collapsed into a single login under a lock,
  and a 429 is retried briefly then surfaced rather than waited out.
- **`list_agents` no longer truncates silently.** The endpoint is cursor-paginated; the
  result now carries `has_more` and `after`.
- **Bearer scheme matching.** The middleware detected the scheme case-insensitively but
  stripped it case-sensitively, so a conformant `bearer <token>` was rejected. Found by
  test. The same bug exists in `nats-mcp` and `memsearch-mcp` — filed as vikunja#669.

### Added

- Standard CI workflow (`ci.yml`) — ruff (pinned `ruff==0.16.0`) + pytest +
  pip-audit + build; release workflow cutting a GitHub Release from a `vX.Y.Z` tag;
  explicit ruff config; `AGENTS.md` and `SECURITY.md`. (Previously unreleased.)
- **Bearer authentication on the MCP endpoint**, `LIBRECHAT_MCP_API_TOKEN`, minimum 16
  characters, failing closed at startup. There was previously **no authentication at all**:
  an unauthenticated `initialize` returned 200 from a throwaway container on
  `librechat-internal`, the network the LibreChat app itself is on, exposing full agent
  CRUD including `delete_agent`. `/health` is exempt by exact path only.
- **`observability.py`** — JSON logs to stdout and opt-in gRPC OTEL on 4317, adapted from
  `nats-mcp` v0.3.0. `structlog.configure()` had never been called anywhere in this
  package: the README's "Structured JSON logs via structlog" was false, `LOG_LEVEL` was
  documented and read by no code, and the container had produced zero application log
  lines in its deployed life.
- `[otel]` extra, installed in CI as `.[dev,otel]` so the OTEL tests cannot skip themselves
  into a green result.
- `HEALTHCHECK`, a multi-stage Dockerfile pinned by digest, `.dockerignore` (the build
  context previously included `.venv/`, `tests/`, `.git/` and any `*.env`), and a
  `deploy/` compose fragment.
- `docker-build` CI job that **runs** the image — asserts it refuses to start without a
  token, goes healthy under `--read-only`, exempts `/health` by exact path while `/healthz`
  and `/health/` still 401, accepts a valid token, rejects a wrong one, and emits a
  structured startup log line.
- LICENSE (MIT) and a `license` field (part of vikunja#583).
- Coverage gate at `fail_under = 90`, with `observability.py` inside it.

### Changed

- `fastmcp` pinned to `>=3.2.0,<4.0.0`. It was `>=2.0`; the container runs 3.4.4 and a
  fresh resolve gives 4.0.3. Since `docker-publish.yml` rebuilds `:latest` on every push to
  `main`, the merge landing this fix would otherwise have shipped an untested major.
- `pydantic` dropped from dependencies — nothing in the package imports it.
- Version single-sourced in `librechat_mcp/__init__.py`; `pyproject.toml` reads it
  dynamically. **This release also reconciles the 0.1.0/0.1.1 drift**: the CHANGELOG had
  released 0.1.1 while `__init__.py`, `pyproject.toml` and the hardcoded UA all still said
  0.1.0. 0.2.0 moves past both.

### Testing

11 tests at 74% coverage → **151 tests at 99%**. Ten mutants were run in an isolated
worktree with green controls either side; all ten were caught, and two of them were fixed
as a result of that run rather than by review.

## [0.1.1] — 2026-06-05

### Fixed
- Add `User-Agent` header to httpx client — LibreChat rejects requests without it
  (HLAGNT-1). **This fix did not work.** The header it added is rejected too; see 0.2.0.
  The tickets it closed (#53-#56) were closed on the diff, without anyone calling a tool
  against the running server.

## [0.1.0] — 2026-06-05

### Added
- FastMCP server wrapping LibreChat REST API for agent CRUD
- JWT auth via email/password with 6-day proactive refresh and 401 retry
- Tools: `list_agents`, `get_agent`, `create_agent`, `update_agent`, `delete_agent`, `list_tools`
- `agent_id` validation against `^[a-zA-Z0-9_-]+$` before path interpolation
- GitHub Actions workflow publishing Docker image to `ghcr.io/tadmstr/librechat-mcp`

### Security
- Non-root `USER app` directive in Dockerfile (F-01)
- Mutable default arguments removed from `create_agent` signature (F-04)
- `list_agents` limit clamped to max 100 (F-05)
- Error messages truncated to 200 chars to prevent internal detail leakage (F-03)
