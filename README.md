# librechat-mcp

FastMCP server for LibreChat agent management.

Wraps the LibreChat REST API to give forge agents programmatic CRUD access to LibreChat agents. Handles JWT authentication transparently — no manual token management needed.

## Tools

| Tool | Description | Key parameters | Returns |
|------|-------------|----------------|---------|
| `list_agents` | List agents, optionally filtered by name | `search` (str), `limit` (int, max 100) | `{agents: [...], count: N, has_more: bool, after?: str}` |
| `get_agent` | Fetch a single agent by ID | `agent_id` (str) | Agent object |
| `create_agent` | Create a new agent | `provider`, `model` (required); `name`, `description`, `instructions`, `tools`, `conversation_starters`, `model_parameters` | Created agent object |
| `update_agent` | Partial update — only include fields to change | `agent_id` + any subset of create fields | Updated agent object |
| `delete_agent` | Delete an agent by ID | `agent_id` | `{deleted: true, agent_id: ...}` |
| `list_tools` | List tool capabilities available for agent creation | — | `{tools: [...]}` |

### `list_agents` returns a projection, not full agents

`GET /api/agents` responds with a subset of each agent: `_id`, `id`, `name`,
`description`, `category`, `author`, `isPublic`, `is_promoted`, `support_contact`,
`updatedAt`. There is **no** `model`, `provider`, `tools` or `instructions`. Call
`get_agent` per row if you need those.

It is also cursor-paginated, and this tool fetches **one page**. The result carries
`has_more`, plus `after` when there are further pages — pass that cursor back to walk
them. Before v0.2.0 the extra pages were dropped with no indication at all.


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

The server is designed to run as a sidecar alongside the LibreChat container, with access to the internal Docker network.

```bash
docker pull ghcr.io/tadmstr/librechat-mcp:latest
```

Minimal compose snippet:

```yaml
services:
  librechat-mcp:
    image: ghcr.io/tadmstr/librechat-mcp:latest
    environment:
      LIBRECHAT_URL: http://LibreChat:3080
      LIBRECHAT_ADMIN_EMAIL: ${LIBRECHAT_ADMIN_EMAIL}
      LIBRECHAT_ADMIN_PASSWORD: ${LIBRECHAT_ADMIN_PASSWORD}
      MCP_PORT: "8496"
    ports:
      - "127.0.0.1:8496:8496"
    restart: unless-stopped
```

### PM2 (forge)

```json
{
  "name": "librechat-mcp",
  "script": "/path/to/.venv/bin/librechat-mcp",
  "env_file": "/path/to/librechat-mcp.env",
  "restart_delay": 5000
}
```

### scoped-mcp wiring

```yaml
- name: librechat-mcp
  url: http://127.0.0.1:8496/mcp
  transport: streamable-http
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
