# Spec: Power BI Model Doctor (Phase 1)

Status: Phase 1, in progress. Builds on `docs/specs/core.md`. Decisions are in ADR 0001–0004.

## Goal

A Power BI developer points Datawright at a model (a PBIP folder in Git, or a `.pbix` file) and, in minutes, gets:

1. a **health check**: a score and a prioritised list of problems (Best Practice Analyzer rules, Power Query refresh risks, AI-readiness);
2. **proposed fixes and documentation** as a change set they review edit by edit;
3. a **model guide** (data dictionary) they can share;
4. **answers to questions about the model** ("which measure is margin?", "where does revenue come from?").

All of this works from Claude Desktop, Claude Code or VS Code through the Datawright MCP server, as well as from the CLI and the local UI.

## How this fills the gaps

| Gap | Phase 1 answer | Later |
|---|---|---|
| 30–50% of time on repetitive work: **documenting models** | Documenter for tables, columns and measures; model guide | Synonyms, AI instructions (Phase 1b, once PBIP storage is confirmed) |
| Repetitive work: **ad-hoc questions** | `ask` answers questions *about the model* (meaning, location, lineage of measures) with cited objects | Number questions via DAX against Desktop (Phase 2) |
| Repetitive work: **debugging refresh/pipeline problems** | Power Query rules for refresh risks (hard-coded local paths, untyped sources) | Diagnose a pasted refresh error with model context and propose an M fix (Phase 2) |
| Repetitive work: **cleaning data** | Power Query type checks | Proposed cleaning steps as M change sets (Phase 4+) |
| **Power BI / enterprise gap** | Works on Pro-licence models with no Fabric capacity; reads `.pbix` | Tenant governance, SSO (Phase 3) |
| **Trust through human-in-the-loop** | Per-edit accept, reject or edit before approval; feedback kept; acceptance metrics; MCP clients can propose but never approve or apply | Approval by a second person (Phase 3) |
| **Synergy with the stack** | MCP server for Claude Desktop/Code/VS Code: the user's own Claude is the brain, and Datawright supplies governed tools, change sets and review. No API key is needed in that mode | Packaged desktop app (Electron or Tauri) |

## Model sources

| Source | Connection kind | Capability | How |
|---|---|---|---|
| PBIP folder in Git | `pbip_repo` | read / propose / apply | TMDL reader (`tmdl.py`) |
| `.pbix` file | `pbix_file` | **read only** | `pbixray` (pinned; metadata only, never table data) |

Both produce the same `SemanticModel` (`connectors/powerbi/model.py`): tables (with description, hidden, data category, partitions, hierarchies), columns (type, kind, key, summarise-by, sort-by, data category, format, source column, expression), measures, relationships (both ends, cardinality, filter direction, active) and model annotations. A `.pbix` connection explains how to unlock changes: *Save as PBIP* in Desktop, then `datawright track <folder>`.

## Rules

Each rule has an id, category, severity (`error` / `warning` / `info`), description, and the Best Practice Analyzer id it ports (if any). The BPA list is MIT-licensed (© Microsoft, see `NOTICE`). Rules find problems; the LLM explains and fixes them.

Categories: Performance, DAX, Error prevention, Maintenance, Formatting, Naming, Power Query (refresh risks), AI readiness.

**Health score** = 100 − (5 × errors + 2 × warnings + 0.5 × info), floored at 0. **AI-readiness** is the share of visible tables, columns and measures that have a description. Both appear in `check`, the UI and MCP.

## Documenter (`document-model`)

- Kinds: measures, columns, tables (visible objects without a description). Tools: `list_objects`, `get_object`, `propose_description`.
- Output: one change set; one item per TMDL file; validated structurally before `proposed`.

## Review (trust)

- Each edit in a proposed change set can be **accepted**, **rejected** or **edited** before approval. Approval rebuilds the items from the accepted and edited intents, then re-validates.
- Decisions are stored on the change set (`feedback`), with counts: proposed, accepted unchanged, edited, rejected. The dashboard shows the acceptance rate.

## Automatic fixes (`datawright fix`)

- Fixable rules: numeric-column-summarize-by, hide-foreign-keys, mark-primary-keys, measure-format-string, sort-month-columns (only when there's exactly one month-number column), add-data-category, mark-date-table (which also resolves model-has-date-table), and use-divide-function (only a plain single-line `A / B`).
- Each fix is one or more TMDL intents (`set_property`, `set_expression`), validated structurally like descriptions. Every edit has its own id, so two edits on the same column can be decided separately.
- Anything that deletes, renames or needs judgement stays manual. Every rule has a "how to fix" note in the CLI, the UI and MCP.
- Invariant (tested): applying all fixes to `retail-issues` removes exactly the fixed findings and introduces none.

## Ask (`ask-model`)

- Read-only tools: `search_model`, `get_object`, `list_relationships`.
- Every answer cites object paths. If the model doesn't contain the answer, it says so.

## MCP server (`datawright mcp`, stdio)

| Tool | Does | Writes? |
|---|---|---|
| `list_connections` | Connections and their capability | no |
| `model_health` | Score, AI-readiness, counts by rule | no |
| `list_findings` | Findings, filterable by rule or severity | no |
| `search_model` / `get_object` | Metadata lookup | no |
| `list_undocumented` | Visible objects without descriptions | no |
| `propose_description` | Adds an edit to the session's draft change set | proposes only |
| `submit_for_review` | Validates the draft; returns the review URL | proposes only |

No MCP tool can approve, apply or roll back. Every MCP call is recorded as an AgentStep on a `mcp-session` run.

## Other

- `datawright check <folder-or-.pbix>`: prints the health check without needing a connection.
- `datawright guide <connection>`: writes the model guide (Markdown). The UI shows a printable version.
- `datawright track <folder>`: `git init` and a first commit, after confirmation.
- Credentials come from the environment or a `.env` file (project folder or data folder). The default data folder is `%LOCALAPPDATA%\Datawright`.

## Exit criteria (from the plan)

Fixtures (and pilot models) open in Power BI Desktop after apply; TMDL golden tests pass; 3 pilots use it on their own models at least twice in two weeks; description acceptance rate measured (start target 60%). The build-side criteria are tests, evals and a working MCP session; the pilot-side ones need Phase A partners.
