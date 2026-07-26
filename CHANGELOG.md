# Changelog

## [Unreleased]

### Added

- Standard CI workflow (`ci.yml`) — ruff (pinned `ruff==0.16.0`) + pytest + pip-audit + build.
- Release workflow (`release.yml`) — a `vX.Y.Z` tag cuts a source-only GitHub Release.
- Explicit ruff config (`select = ["E", "F", "W", "I", "UP", "B", "SIM", "RUF"]`) pinning the
  enforced ruleset against ruff's widening defaults.
- Standard repo docs: `AGENTS.md`, `SECURITY.md`.

### Removed

- `test.yml` workflow — superseded by the fuller `ci.yml`.

## [0.1.1] — 2026-06-05

### Fixed
- Add `User-Agent` header to httpx client — LibreChat rejects requests without it (HLAGNT-1)

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
