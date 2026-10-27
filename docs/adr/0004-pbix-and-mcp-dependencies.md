# ADR 0004: `.pbix` reading and the MCP server

- Status: accepted
- Date: 2026-10-27

## Context

Most Power BI users have `.pbix` files, not PBIP folders in Git (REVIEW.md 3.6). The plan also moved the Datawright MCP server from Phase 4 to Phase 1, so users can work from Claude Desktop, Claude Code or VS Code.

## Decision

- **`pbixray` (MIT), pinned to `>=0.15.5,<0.16`**, reads `.pbix` model metadata. Its public frames lack measure format strings, hidden flags, sort-by columns and inferred types of calculated columns, so `connectors/powerbi/pbix.py` queries the engine's metadata tables through pbixray's internal reader. The pin protects that internal access, and `tests/test_pbix.py` runs against a real Microsoft sample to catch breakage when the pin moves. We never call pbixray's table-data functions.
- **A `.pbix` connection is read-only.** Datawright doesn't rewrite `.pbix` files. To make changes, save as PBIP and run `datawright track`.
- **`mcp` (official MCP Python SDK, MIT), `>=2.3,<3`**, runs the stdio server via `MCPServer` (the v2 name for FastMCP). The server exposes read and propose tools only; there are no approve, apply or rollback tools, and `tests/test_mcp.py` asserts that.

## Consequences

- Upgrading pbixray means re-running `test_pbix.py` and checking the SQL in `pbix.py`.
- In MCP mode the client's own model does the reasoning, so no Anthropic API key is needed on the user's machine.
