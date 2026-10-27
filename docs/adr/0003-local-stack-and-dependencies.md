# ADR 0003: Local stack and dependencies

- Status: accepted
- Date: 2026-10-26

## Decision

| Concern | Choice | Why | Revisit |
|---|---|---|---|
| Language | Python 3.11+ (code also runs on 3.12) | 3.11 is what's installed on the dev machine; the data libraries all support it | — |
| Layout | One package `src/datawright` with subpackages (core, connectors, agents, evals, web) | A small team doesn't need a multi-package workspace yet | When a second deployable appears (cloud API, desktop agent) |
| Storage | SQLite file via SQLAlchemy 2, Alembic migrations | Local-first: no database server to install. SQLAlchemy keeps the move to Postgres a config change | Phase 3 (cloud): Postgres + pgvector |
| Web UI | FastAPI + Jinja2 server-rendered pages, plain HTML forms, no JavaScript build | One process, works offline, fewer moving parts; review pages are mostly forms and diffs | When we need rich interaction (lineage graph, Monaco diff) |
| CLI | Typer | The plan's choice; also runs the demo and evals | — |
| TMDL | Our own reader plus targeted text edits (e.g. the `///` description lines), byte-for-byte everywhere else | No mature Python TMDL parser exists; text edits give clean diffs and keep the user's formatting | If edits outgrow text: Power BI Modeling MCP or TOM (.NET) |
| Git | The `git` CLI through `subprocess` | It's already on every dev machine; avoids a dependency | — |
| Packaging | `venv` + `pip install -e ".[dev]"` | `uv` isn't installed on the dev machine | Adopt `uv` and a lockfile before the first pilot install |
| Background jobs | None (runs are synchronous) | Phase 0–2 runs are short and user-triggered | Phase 3: a job queue for scheduled crawls and narratives |

## Runtime dependencies (one line each, as CLAUDE.md requires)

- `anthropic`: Claude API client (ADR 0002).
- `fastapi`, `uvicorn`, `jinja2`, `python-multipart`: local review UI and form posts.
- `sqlalchemy`, `alembic`: storage and migrations.
- `pydantic`, `pydantic-settings`: typed settings and schemas.
- `typer`: CLI.
- `pyyaml`: eval datasets and thresholds.

Dev only: `pytest`, `httpx` (FastAPI test client), `ruff`, `mypy`, `types-PyYAML`.
