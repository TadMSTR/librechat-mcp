# librechat-mcp

FastMCP server for LibreChat agent management.

Wraps the LibreChat REST API to give forge agents programmatic CRUD access to LibreChat agents. Handles JWT authentication transparently — no manual token management needed.

## Tools

| Tool | Description | Key parameters | Returns |
|------|-------------|----------------|---------|
| `list_agents` | List agents, optionally filtered by name | `search` (str), `limit` (int, max 100) | `{agents: [...], count: N}` |
| `get_agent` | Fetch a single agent by ID | `agent_id` (str) | Agent object |
| `create_agent` | Create a new agent | `provider`, `model` (required); `name`, `description`, `instructions`, `tools`, `conversation_starters`, `model_parameters` | Created agent object |
| `update_agent` | Partial update — only include fields to change | `agent_id` + any subset of create fields | Updated agent object |
| `delete_agent` | Delete an agent by ID | `agent_id` | `{deleted: true, agent_id: ...}` |
| `list_tools` | List tool capabilities available for agent creation | — | `{tools: [...]}` |

**agent_id** must match `^[a-zA-Z0-9_-]+$`. IDs come from `list_agents` or `create_agent`.

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `LIBRECHAT_URL` | Yes | — | LibreChat base URL, e.g. `http://librechat:3080` |
| `LIBRECHAT_ADMIN_EMAIL` | Yes | — | Admin account email for JWT login |
| `LIBRECHAT_ADMIN_PASSWORD` | Yes | — | Admin account password for JWT login |
| `MCP_PORT` | No | `8496` | Port to bind (streamable-http transport) |

Authentication uses LibreChat's email/password login endpoint (`POST /api/auth/login`). The JWT is cached in-process and refreshed proactively after 6 days (LibreChat default token lifetime is 7 days). A 401 response triggers an immediate re-login.

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

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to forward traces to SigNoz or another collector if structlog OTEL output is wired in.
