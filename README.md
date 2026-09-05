# librechat-mcp

FastMCP server for LibreChat agent management.

Wraps the LibreChat REST API to give forge agents programmatic CRUD access to LibreChat agents. Handles JWT authentication transparently — no manual token management needed.

## Tools

22 tools in three groups. Every one returns a **dict** — see the return convention
below.

### Agents

| Tool | Description | Key parameters |
|------|-------------|----------------|
| `list_agents` | List agents, following the pagination cursor by default | `search`, `limit`, `expand`, `follow_cursor` |
| `get_agent` | Fetch one agent | `agent_id`, `expanded` |
| `create_agent` | Create an agent — 28 writable fields | `provider`, `model` (required) + any writable field, `mcp_servers` |
| `update_agent` | Partial update; only supplied fields are sent | `agent_id` + any subset of the create fields |
| `delete_agent` | Delete an agent | `agent_id` |
| `ensure_agent` | **Idempotent create-or-update by name** | `name`, `spec`, `mcp_servers`, `create_missing` |
| `duplicate_agent` | Copy an agent, instructions and tools included | `agent_id` |
| `list_agent_versions` | An agent's saved versions, oldest first | `agent_id` |
| `revert_agent` | Restore a saved version | `agent_id`, `version_index` |

### Access control

| Tool | Description | Key parameters |
|------|-------------|----------------|
| `get_agent_permissions` | Who can use this agent | `agent_id` |
| `share_agent` | Grant or revoke access | `agent_id`, `grant`, `revoke`, `public_access_role_id` |
| `search_principals` | Find users/groups/roles to grant to | `query`, `limit` |
| `get_resource_access_roles` | Valid `accessRoleId` values for a resource type | `resource_type` |
| `get_effective_permissions` | What the calling account can actually do | `agent_id` (optional) |
| `get_role_permissions` | A role's full feature matrix | `role` |
| `set_role_permissions` | Change one permission type on a role | `role`, `permission_type`, `permissions` |

### Discovery and validation

| Tool | Description | Key parameters |
|------|-------------|----------------|
| `list_tools` | LibreChat's **built-in** tools | — |
| `list_mcp_tools` | MCP server tools, keyed by server | `server` |
| `list_mcp_servers` | Configured MCP servers with connection state | — |
| `list_models` | Model ids per provider, enabled providers only | `enabled_only` |
| `list_categories` | Agent-picker categories with counts | — |
| `validate_agent_spec` | Check a spec against live lookups before writing it | `provider`, `model`, `tools`, `mcp_servers`, `category` |

### Every tool returns a dict

List-shaped results are wrapped as `{"<plural>": [...], "count": N}`. This is not
cosmetic. FastMCP builds each tool's output schema from its return annotation and
refuses to serialise a bare list, so a `-> dict` tool returning one fails **on a
successful upstream response**. `list_tools` did exactly that and was 100% dead
through MCP for the whole of v0.2.0 while its unit test stayed green — that test
called the undecorated function and never crossed the boundary that breaks
(vikunja#672).

Four LibreChat endpoints return bare arrays: `/api/agents/tools`,
`/api/agents/categories`, `/api/agents/<id>/versions` and
`/api/permissions/agent/roles`. All four are wrapped here, and
`tests/test_mcp_layer.py` invokes every registered tool through the MCP layer to keep
it that way.

## Things that will bite you

Each of these was measured against a running LibreChat v0.8.8-rc2, and each is a case
where the API succeeds and does not do what you asked.

### `mcpServerNames` cannot be set — use `mcp_servers`

It is absent from LibreChat's `agentCreateSchema`/`agentUpdateSchema` and is *derived*
server-side from whichever `tools` entries carry the `_mcp_` delimiter:

```
tools=['search_mcp_searxng'], no mcpServerNames  ->  mcpServerNames: ['searxng']
mcpServerNames=['searxng'], no MCP tools         ->  mcpServerNames: []
```

So `create_agent(mcp_servers=['searxng'])` expands to that server's tool pluginKeys.
Passing `mcpServerNames` yourself returns 201 and changes nothing.

### Unknown tools are dropped silently on a 201

`filterAuthorizedTools` removes any tool key it does not recognise or authorise, and
the request still succeeds — so the agent is created *without* the capability it was
created for. `create_agent` and `update_agent` report this as `dropped_tools` plus a
`warning`; `validate_agent_spec` catches it beforehand.

### Fields LibreChat accepts and ignores

`isPublic`, `is_promoted`, `access_level` and `tool_kwargs` are not in the zod schemas,
so they are stripped silently on a 200/201. They are deliberately **not** exposed as
parameters. Use `share_agent` for sharing — the ACL surface is the layer that works.

### The ACL routes are keyed by the Mongo `_id`

`/api/permissions/*` does not take the public `agent_...` id, and mostly does not say
so:

| call | with the agent `id` | with `_id` |
|---|---|---|
| `GET /api/permissions/agent/<x>` | 200, `principals: []` | 200, the real principals |
| `GET .../effective` | `{permissionBits: 0}` | `{permissionBits: 15}` |
| `PUT /api/permissions/agent/<x>` | 400 `Invalid resource ID` | 200 |

The tools here take the public id and resolve `_id` internally. If you call the API
directly, note that two of those three answer with a confident wrong answer rather
than an error.

### Pagination: the response says `after`, the request wants `cursor`

The envelope reports the next cursor as `after`, but the handler reads
`req.query.cursor`. Sending it back as `after=` is an unknown parameter, so it is
ignored and every page is page one — with `has_more: true`, forever. `list_agents`
sends `cursor`, and additionally stops when a page yields no new ids.

### `list_agents` returns a projection

`_id`, `id`, `name`, `description`, `category`, `author`, `isPublic`, `is_promoted`,
`support_contact`, `owner_contact`, `isEditable`, `updatedAt`. There is **no** `model`,
`provider`, `tools` or `instructions`. Pass `expand=true` (an N+1 fan-out) when you
need something to diff a spec against.

### List and dict fields replace, they do not merge

`update_agent(tools=['calculator'])` on an agent with three tools leaves it with one.
Read the current list with `get_agent` and post the union.

### `memory_scope` and `skills_scope` are different vocabularies

`memory_scope` is `'user' | 'agent'` — **not** a boolean. `'agent'` is the Agent
Builder's "Keep memories separate for this agent". `skills_scope` is
`'all' | 'selected' | 'none'`. Confusing the two gets a 400.

## Provisioning a fleet

`ensure_agent` is the declarative primitive: define each agent once and re-apply until
it stops changing anything.

```python
ensure_agent(
    name="Research",
    spec={
        "provider": "Mistral",              # a custom endpoint's `name:` verbatim
        "model": "mistral-small-latest",
        "instructions": "You research things and cite sources.",
        "category": "general",
        "memory_scope": "agent",            # isolate this agent's memories
    },
    mcp_servers=["searxng"],
)
# -> {"action": "created",   "agent_id": "agent_...", "changed": [...]}
# -> {"action": "unchanged", "agent_id": "agent_...", "changed": []}   on a re-run
```

`unchanged` means **no write was issued** — the current agent is fetched and diffed
first. That matters because LibreChat appends to `versions[]` on a real change and
`revert_agent` walks that list, so a reconciler that PATCHed unconditionally would
bury every meaningful revision under identical re-applications.

Then share it:

```python
principal = search_principals(query="Patrick")["results"][0]
share_agent(
    agent_id="agent_...",
    grant=[{"type": "user", "id": principal["id"], "accessRoleId": "agent_viewer"}],
)
```

Grants are named by `accessRoleId` (`get_resource_access_roles` lists them), never by a
bitmask. `grant` and `revoke` are separate arguments because omitting a principal does
**not** revoke it.

Two things to know before granting: Editor and Owner grantees can see the agent's
system instructions, files and tools, so sharing discloses configuration as well as
use; and an agent's memory partition is anchored to agent access, so revoking a share
can strand that user's memories for it.

**agent_id** must match `^[a-zA-Z0-9_-]+$`. IDs come from `list_agents` or `create_agent`.

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `LIBRECHAT_URL` | Yes | — | LibreChat base URL, e.g. `http://librechat:3080` |
| `LIBRECHAT_ADMIN_EMAIL` | Yes | — | Admin account email for JWT login |
| `LIBRECHAT_ADMIN_PASSWORD` | Yes | — | Admin account password for JWT login |
| `MCP_PORT` | No | `8496` | Port to bind (streamable-http transport) |
| `LIBRECHAT_MCP_API_TOKEN` | **Yes** | — | Bearer token for the MCP endpoint. Minimum 16 chars; the server refuses to start without it |
| `LOG_LEVEL` | No | `INFO` | Logging verbosity |
| `LOG_FILE` | No | unset | Optional file sink. Leave unset in a container — logs go to stdout |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | unset | Enables OTEL. **gRPC, port 4317** — not 4318 |

### Authentication to LibreChat

Uses LibreChat's email/password login endpoint (`POST /api/auth/login`). The JWT is
cached in-process and refreshed 60 seconds before the `exp` claim in the token itself,
with a 401 response triggering an immediate re-login as a backstop.

**The token lives 900 seconds — 15 minutes.** Every version of this document before
v0.2.0 said 7 days, and the code refreshed after 6 days, which meant the proactive
refresh never fired once in production. Measured by decoding a live token on LibreChat
v0.8.5 and again on v0.8.8-rc2. The refresh now reads `exp` from the token rather than
trusting a number written here, so it cannot go stale the same way.

`POST /api/auth/login` is rate-limited at roughly 4 attempts per 5 minutes. Concurrent
tool calls arriving on an expired token are collapsed into a single login, and a 429 is
retried briefly and then surfaced rather than waited out — a five-minute sleep inside a
tool call reads to the caller as a hang.

### Authentication to *this* server

The MCP endpoint requires `Authorization: Bearer $LIBRECHAT_MCP_API_TOKEN` on every
request. `/health` is the sole exemption, matched by exact path so `/healthz` and
`/health/` still 401, and its body carries a bare status and no configuration.

The server **refuses to start** without a token of at least 16 characters. That is
deliberate: it binds `0.0.0.0` inside its container, and before v0.2.0 an
unauthenticated `initialize` was verified returning 200 from a throwaway container on
`librechat-internal` — the same network the LibreChat app is on. Full agent CRUD,
including `delete_agent`, was reachable by anything co-resident there.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## Installation

**Requirements:** Python 3.11+

```bash
git clone https://github.com/TadMSTR/librechat-mcp
cd librechat-mcp
python -m venv .venv
source .venv/bin/activate
pip install .
```

### Run (stdio)

```bash
export LIBRECHAT_URL=http://localhost:3080
export LIBRECHAT_ADMIN_EMAIL=admin@example.com
export LIBRECHAT_ADMIN_PASSWORD=secret
librechat-mcp
```

### Run (HTTP transport)

```bash
MCP_PORT=8496 librechat-mcp
```

## Deployment

### Docker sidecar

The server runs as a sidecar alongside the LibreChat container, on the internal Docker
network.

**A full, hardened service block is in [`deploy/docker-compose.librechat-mcp.yml`](deploy/docker-compose.librechat-mcp.yml)** — use that
rather than the snippet below if you are deploying for real. It carries `read_only`,
`cap_drop`, `no-new-privileges`, resource limits, log rotation, and the reasoning for each.

```bash
# note: no `v` — the git tag is v0.2.0, the image tag is 0.2.0
docker pull ghcr.io/tadmstr/librechat-mcp:0.2.0
```

Minimal compose snippet:

```yaml
services:
  librechat-mcp:
    # Pin the tag. `:latest` also exists, but it moves on every push to main while the
    # running container does not — so what is deployed and what is running can disagree.
    image: ghcr.io/tadmstr/librechat-mcp:0.2.0
    environment:
      # The service name on the shared network, NOT localhost — that is this
      # container's own loopback, where nothing is listening.
      LIBRECHAT_URL: http://librechat:3080
      LIBRECHAT_ADMIN_EMAIL: ${LIBRECHAT_ADMIN_EMAIL}
      LIBRECHAT_ADMIN_PASSWORD: ${LIBRECHAT_ADMIN_PASSWORD}
      MCP_PORT: "8496"
      # MANDATORY from v0.2.0. The server refuses to start without it, so omitting
      # this line does not give you an unauthenticated server — it gives you no
      # server. Minimum 16 characters.
      LIBRECHAT_MCP_API_TOKEN: ${LIBRECHAT_MCP_TOKEN}
    ports:
      - "127.0.0.1:8496:8496"
    restart: unless-stopped
```

**Upgrading from v0.1.x is a paired change.** Add the token to your `.env` *before* pulling,
or the container will fail to start:

```bash
# 1. add LIBRECHAT_MCP_TOKEN=<32+ hex chars> to the stack .env (mode 600)
# 2. then, and only then:
docker compose pull && docker compose up -d librechat-mcp
```

`docker compose restart` will **not** pick up a new image, and pushing to `main` republishes
`:latest` without redeploying anything. A "fixed" service stays broken in that gap.

### PM2 (forge)

```json
{
  "name": "librechat-mcp",
  "script": "/path/to/.venv/bin/librechat-mcp",
  "env_file": "/path/to/librechat-mcp.env",
  "//": "the env file must set LIBRECHAT_MCP_API_TOKEN — the process refuses to start without it",
  "restart_delay": 5000
}
```

### scoped-mcp wiring

```yaml
- name: librechat-mcp
  url: http://127.0.0.1:8496/mcp
  transport: streamable-http
  headers:
    # Required from v0.2.0 — without it every call returns 401.
    Authorization: "Bearer ${LIBRECHAT_MCP_TOKEN}"
```

## Usage examples

```
# List all agents
list_agents()

# Search for agents by name
list_agents(search="sysadmin", limit=5)

# Get agent details
get_agent(agent_id="abc123")

# Create an agent
create_agent(
  provider="anthropic",
  model="claude-sonnet-4-6",
  name="Research Assistant",
  instructions="You help with technical research.",
  tools=["web_search", "artifacts"]
)

# Update an agent's model
update_agent(agent_id="abc123", model="claude-opus-4-6")

# Delete an agent
delete_agent(agent_id="abc123")

# Discover available tool capabilities
list_tools()
```

## Observability

Structured JSON logs via `structlog`. Key log events:

| Event | When |
|-------|------|
| `librechat_auth_ok` | Successful JWT login |
| `librechat_token_expired_refreshing` | 401 triggered re-login |
| `list_agents` | Tool called, includes `count` and `search` fields |
| `create_agent` | Agent created, includes `name`, `provider`, `model` |
| `update_agent` | Agent updated, includes `agent_id` and `fields` changed |
| `delete_agent` | Agent deleted, includes `agent_id` |
| `tool_error` | Any tool returned an error, includes `tool` and `error` |

| `librechat_mcp_starting` | Startup, includes `port` and `version` |
| `librechat_login_rate_limited` | A 429 from the login endpoint, with the backoff delay |

Logs go to **stdout** as JSON, which is what `docker logs` and Loki read and what makes
`read_only: true` viable with no writable path. Set `LOG_FILE` to add a file sink; leave
it unset in a container. Third-party loggers (`httpx`, `mcp`, `fastmcp`, `uvicorn`,
`starlette`) are demoted to WARNING so a wire trace of every call does not drown the
records that matter.

> Before v0.2.0 `structlog.configure()` was never called anywhere in this package. The
> claim above was simply false and the container had emitted zero application log lines
> in its deployed life, which is a large part of why every tool could fail for six weeks
> without anyone noticing.

### OpenTelemetry

Opt-in. Install the extra and set the endpoint:

```bash
pip install '.[otel]'
export OTEL_EXPORTER_OTLP_ENDPOINT=http://signoz-otel-collector:4317
```

Exports one span per tool call plus two metrics — `librechat_mcp.tool.calls` (counter,
labelled by tool and outcome) and `librechat_mcp.tool.duration` (histogram).

**The endpoint is gRPC on 4317, not HTTP on 4318.** Pointing it at 4318 fails silently.
So does setting the variable without the `[otel]` extra installed: the exporter import
fails once and nothing is ever exported. The published image installs the extra.
