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

## Copilot "Prep data for AI" pack (`prep-for-ai`, `datawright prep-ai`)

**Format (sources, not guesses).** A PBIP model keeps its Copilot settings in `<Model>.SemanticModel/Copilot/`. The folder layout comes from the Fabric semantic model definition docs; three files have official JSON schemas in `microsoft/json-schemas` (MIT; copies in `tests/data/ms-schemas/`); file names, schema versions and limits were read from Power BI Desktop 2.159's `Microsoft.PowerBI.Packaging.dll` (string table and `const` fields, read without running it).

| File | Content | Datawright |
|---|---|---|
| `Copilot/schema.json` | AI data schema: `tables[]` with `columns[]`, `measures[]`, `hierarchies[]`; each entity has `name` (required, ≤512), optional `visibility` (`Auto`/`Visible`/`Hidden`) and `synonyms` (≤1,024 items, each ≤1,024 chars) | writes synonyms |
| `Copilot/Instructions/instructions.md` | AI instructions, plain Markdown, ≤15,000 characters | writes |
| `Copilot/examplePrompts.json` | `prompts[]`, ≤500 characters each | writes |
| `Copilot/settings.json`, `Copilot/version.json`, `Copilot/Instructions/version.json` | settings (`indexingEnabled`) and version markers | never writes |
| `Copilot/VerifiedAnswers/` | verified answers (visual snapshot packages) | never writes: they need a visual and a sign-in |

Every JSON file starts with its `$schema` URL (`…/semanticModel/copilot/<name>/1.0.0/schema.json`).

**Verified in Desktop 2.159 (2026-10-29):** a project with a `Copilot` folder holding only `schema.json`, `examplePrompts.json` and `Instructions/instructions.md` (no `version.json`) opens without error, Desktop watches the folder for external changes, and a Desktop save keeps the folder byte for byte. **Not yet verified:** that Copilot reads these files. Opening *Prep data for AI* needs a sign-in to a Copilot-enabled account (a work account on Fabric F2+ or Premium). Until that check passes, the CLI and UI label the pack "preview".

**Edits** (change-set intents, like TMDL edits):
- `copilot.set_synonyms` `{table, kind: table|column|measure, name, synonyms[]}` on `schema.json`: sets that entity's synonyms, creating the file, table or entity entry if needed. An empty list removes the `synonyms` key. Other entities, their order and unknown keys are kept.
- `copilot.set_instructions` `{value}` on `instructions.md`: replaces the whole text (Markdown, LF endings, final newline).
- `copilot.set_example_prompts` `{value: [..]}` on `examplePrompts.json`.
- JSON files are written as UTF-8, 2-space indent, LF, final newline. The structural check re-reads `after`, validates it against the limits above, and requires that only the targeted entity (or file) changed.
- A change set can create these files: an item may start from a missing file (`before` = ""). Apply then requires the file to still be missing at the base, and rollback deletes it again.

**Agent `prep-for-ai`** (skill `skills/powerbi/prep-for-ai.md`): tools `list_objects`, `get_object`, `get_copilot_settings` (read), `propose_synonyms`, `propose_ai_instructions`, `propose_example_prompts` (propose). Synonyms go to visible measures, tables and columns that users ask about by other names; AI instructions explain business terms, default measures, date logic and what the model does not contain. Metadata only, never data rows. `--offline` uses scripted drafts (plural/singular forms, names without "Total") for demos and tests. The MCP server exposes the same read and propose tools.

## Review (trust)

- Each edit in a proposed change set can be **accepted**, **rejected** or **edited** before approval. Approval rebuilds the items from the accepted and edited intents, then re-validates.
- Decisions are stored on the change set (`feedback`), with counts: proposed, accepted unchanged, edited, rejected. The dashboard shows the acceptance rate.

## Automatic fixes (`datawright fix`)

- Fixable rules: numeric-column-summarize-by, hide-foreign-keys, mark-primary-keys, measure-format-string, sort-month-columns (only when there's exactly one month-number column), add-data-category, mark-date-table (which also resolves model-has-date-table), and use-divide-function (only a plain single-line `A / B`).
- Each fix is one or more TMDL intents (`set_property`, `set_expression`), validated structurally like descriptions. Every edit has its own id, so two edits on the same column can be decided separately.
- Anything that deletes, renames or needs judgement stays manual. Every rule has a "how to fix" note in the CLI, the UI and MCP.
- Invariant (tested): applying all fixes to `retail-issues` removes exactly the fixed findings and introduces none.

## Report usage

- `connectors/powerbi/report.py` reads which fields visuals, page filters and report filters use: PBIR folders in PBIP projects, the older single-file `report.json`, and the report inside a `.pbix` (PBIR or the classic `Report/Layout`, including aliased `From` lists and JSON-string configs).
- A PBIP report is linked to its model through `definition.pbir` (`byPath`); with a single model, it's linked to that model.
- Rules use it: hidden columns and measures used by a visual are no longer reported as unnecessary, and two info rules, `unused-measure` and `unused-column`, flag visible objects that no measure, relationship or visual uses. They only run when a report is present, and their message notes that other reports on the same model aren't checked.
- `diff` reports removed or renamed fields that visuals still use (`broken_visuals`, with the report, page and visual type), and `--fail-on-break` fails on them.
- Fixture `retail-report` = the clean model plus a PBIR report that uses every visible field except [Order Lines] and Customer[Segment].

## Semantic diff (`datawright diff`)

- Compares two Git revisions of a PBIP project (or a revision against the working tree) in model terms: tables, columns, measures and relationships added, removed or changed (field by field), plus the health-score delta and findings resolved or introduced.
- **Broken references:** the `unknown-reference` rule (error) flags DAX that points at a missing column or measure. It skips names an expression creates itself (ADDCOLUMNS aliases, `[Value]`/`[Date]` from GENERATESERIES or CALENDAR) and `X[...]` where X isn't a model table (variables). It had zero false positives on the three Microsoft samples.
- `--out` writes Markdown for a pull-request comment; `--fail-on-break` exits 1 for CI.
- The change-set page shows the same preview ("What this changes") before approval, and the base-to-commit diff after apply.

## Refresh doctor (`datawright diagnose`)

- A library of 15 refresh-error patterns (connectors/powerbi/refresh.py). Each has a cause, a fix and a *locator* that finds the implicated objects in the user's model: M partitions and steps (parsed into named steps), model columns loaded from a source column, relationships whose "one" side has duplicate or blank keys, sheet/table navigation steps, web hosts, on-premises sources, and unused columns for memory errors.
- Deterministic and offline. `--explain` (CLI) runs Claude with read-only tools (`search_model`, `get_object`, `list_relationships`, `get_query`), grounded in the deterministic diagnosis.
- Also available as a UI page (`/diagnose`) and the MCP tools `diagnose_refresh_error` and `get_query`.
- Eval: `refresh-doctor-v0` (11 deterministic cases: pattern plus evidence objects).

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
| `get_copilot_settings` | A model's Copilot instructions, prompts, synonyms and vocabulary | no |
| `propose_synonyms` / `propose_ai_instructions` / `propose_example_prompts` | Copilot pack edits in the session's draft (same checks as the agent) | proposes only |
| `submit_for_review` | Validates the draft; returns the review URL | proposes only |

Verified 2026-10-29 with a live stdio session (MCP Python SDK 2.3 client → `datawright mcp`): 15 tools listed; a Copilot pack (instructions, 5 prompts, synonyms for 10 objects) proposed and submitted as one change set.

No MCP tool can approve, apply or roll back. Every MCP call is recorded as an AgentStep on a `mcp-session` run.

## Other

- `datawright check <folder-or-.pbix>`: prints the health check without needing a connection.
- `datawright guide <connection>`: writes the model guide (Markdown). The UI shows a printable version.
- `datawright track <folder>`: `git init` and a first commit, after confirmation.
- Credentials come from the environment or a `.env` file (project folder or data folder). The default data folder is `%LOCALAPPDATA%\Datawright`.

## Exit criteria (from the plan)

Fixtures (and pilot models) open in Power BI Desktop after apply; TMDL golden tests pass; 3 pilots use it on their own models at least twice in two weeks; description acceptance rate measured (start target 60%). The build-side criteria are tests, evals and a working MCP session; the pilot-side ones need Phase A partners.
