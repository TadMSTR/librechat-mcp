# librechat-mcp

FastMCP server wrapping the LibreChat REST API. Gives forge agents programmatic CRUD
access to LibreChat agents, handling JWT authentication transparently.

## What it does

Provides 6 tools for listing, reading, creating, updating, and deleting LibreChat agents,
plus a capability lookup used when composing new agents.

## Tools

- `list_agents` — List agents, optionally filtered by `search` (max 100).
- `get_agent` — Fetch a single agent by ID.
- `create_agent` — Create an agent (`provider`, `model` required).
- `update_agent` — Partial update; only supplied fields change.
- `delete_agent` — Delete an agent by ID.
- `list_tools` — List tool capabilities available for agent creation.

`agent_id` must match `^[a-zA-Z0-9_-]+$`. IDs come from `list_agents` or `create_agent`.

## Structure

```
librechat_mcp/
  __init__.py   Package marker, __version__
  server.py     FastMCP setup, tool handlers, main() entry point
  client.py     LibreChatClient — async httpx wrapper, JWT login + refresh
tests/          pytest tests
pyproject.toml
```

## Dependencies

| Package   | Role                    |
|-----------|-------------------------|
| fastmcp   | MCP server framework    |
| httpx     | Async HTTP client       |
| pydantic  | Response models         |
| structlog | JSON structured logging |

## Configuration

| Env var                    | Required | Default  | Purpose                                   |
|----------------------------|----------|----------|-------------------------------------------|
| `LIBRECHAT_URL`            | Yes      | —        | LibreChat base URL                        |
| `LIBRECHAT_ADMIN_EMAIL`    | Yes      | —        | Admin account email for JWT login         |
| `LIBRECHAT_ADMIN_PASSWORD` | Yes      | —        | Admin account password for JWT login      |
| `MCP_PORT`                 | No       | `8496`   | Port to bind (streamable-http transport)  |
| `LOG_LEVEL`                | No       | `INFO`   | Logging verbosity                         |

## Key architecture decisions

- **Transparent JWT handling** — the client logs in via `POST /api/auth/login`, caches the
  JWT in-process, refreshes proactively after 6 days (LibreChat's default token lifetime is
  7 days), and re-logs in on a 401. Callers never manage tokens.
- **Full CRUD surface** — unlike read-only forge MCP servers, this one exposes create/update/
  delete because managing agents is its whole purpose. `agent_id` validation guards the path.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

## Git workflow

Branch before editing — do not commit directly to `main`.
