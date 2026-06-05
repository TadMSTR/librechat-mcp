# librechat-mcp

FastMCP server for LibreChat agent management.

Provides forge agents with programmatic CRUD access to LibreChat agents via the LibreChat REST API.

## Tools

| Tool | Description |
|------|-------------|
| `list_agents` | List agents with optional search and limit |
| `get_agent` | Get a single agent by ID |
| `create_agent` | Create a new agent |
| `update_agent` | Partial update of an existing agent |
| `delete_agent` | Delete an agent by ID |
| `list_tools` | List available LibreChat agent capabilities |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIBRECHAT_URL` | Yes | LibreChat base URL (e.g. `http://librechat:3080`) |
| `LIBRECHAT_ADMIN_EMAIL` | Yes | Admin account email |
| `LIBRECHAT_ADMIN_PASSWORD` | Yes | Admin account password |
| `MCP_PORT` | No | Port to bind (default `8496`) |

## Deploy

Runs as a Docker sidecar alongside LibreChat. See the librechat stack compose file.

## Image

```
ghcr.io/tadmstr/librechat-mcp:latest
```
