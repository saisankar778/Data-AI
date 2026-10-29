# Spec: core platform (Phase 0)

Status: Phase 0. Decisions are in ADR 0001–0003.

## Goal

The smallest platform that can safely carry out one complete loop:

> read a PBIP model → find a problem → an agent proposes a fix → a person reviews the diff → the fix is committed to a Git branch → it can be rolled back

Every later module (auditor, documenter, DAX helper, Ask, Narratives) reuses this loop.

## Entities (SQLite via SQLAlchemy, `src/datawright/models.py`)

| Entity | Key fields | Notes |
|---|---|---|
| Workspace | name | One local workspace, created on first run |
| User | name, role (`viewer`, `analyst`, `developer`, `admin`) | One local user `local`, role `admin` |
| Connection | name, kind (`pbip_repo`), path, capability (`read`/`propose`/`apply`), status | `path` is a Git repo holding one or more `*.SemanticModel` folders |
| Asset | kind (`semantic_model`, `table`, `column`, `measure`, `relationship`), object_path, name, description, file_path, meta (JSON) | Filled by the crawler. `object_path` example: `Retail/Sales/measure/Total Sales`. A `/` or `\` inside a name is escaped with `\` |
| AgentRun | module, prompt, permission, status, model, input/output/cache tokens, cost_usd, final_text, error | One per task. Its change set points back via `ChangeSet.run_id` |
| AgentStep | run_id, seq, kind (`model`, `tool_call`, `check`), tool, input (JSON), output_summary, is_error, duration_ms | The trace. Never stores secrets |
| ChangeSet | connection_id, run_id, title, status, base_sha, branch, commit_sha, review_comment, reviewed_by | The only path to a write |
| ChangeItem | change_set_id, file_path, intents (JSON list of edit ops), before, after, diff | One item per file; edits to the same file accumulate. Full before/after text, so rollback never depends on Git history |
| Finding | connection_id, asset_object_path, source (`audit`), rule, severity (`info`, `warning`, `error`), message, status (`open`, `fixed`, `dismissed`), change_item_id | |
| EvalResult | eval_set, case_id, passed, score, detail, model, git_sha | |
| AuditLog | actor, action, object_type, object_id, detail (JSON) | Insert-only. No update or delete code path exists |

## Capabilities

`Capability` is ordered: `READ < PROPOSE < APPLY`.

- A connection has a maximum capability. The default is `propose`.
- Every agent tool declares the capability it needs. A run gets a permission no higher than its connection's, and **the agent only sees tools at or below the run's permission.**
- No agent tool ever has `APPLY`. Writes happen only in `ChangeSetService.apply()`, after approval by a user whose role is `developer` or `admin`, on a connection with `apply` capability.

## Change set lifecycle

```
draft ──validate──▶ proposed ──approve──▶ approved ──apply──▶ applied ──rollback──▶ rolled_back
                        └──reject──▶ rejected
```

- **draft:** agent tools add items. The `before` text is read from the working tree when the item is created.
- **validate → proposed:** for TMDL items, re-parse `after` and confirm that the targeted object changed as intended and nothing else in the file changed. This is a structural comparison, not just a diff. A failed check leaves the set in `draft`, and the failure is recorded as a `check` step.
- **approve / reject:** requires a comment on reject. Rejections are kept, as future eval cases.
- **apply:** builds a commit **without touching the user's working tree or index**. It uses a temporary index (`GIT_INDEX_FILE`), `hash-object --path`, `update-index`, `write-tree`, `commit-tree`, then `update-ref refs/heads/datawright/cs-<id>`. Before writing, each item's `before` must match the file at `HEAD`. If it doesn't, apply fails with a conflict and the set stays `approved`.
- **stacking:** the parent is the current `HEAD`, unless the connection already has an applied change set whose branch is unmerged work on top of `HEAD` (`HEAD` is a strict ancestor of its tip). Then the parent is the tip of the most recent such branch, and the set is *stacked* on it. The approved intents are replayed onto each file as it is at that tip, re-validated with the same structural check as `propose`, and the item's `before`, `after` and `diff` are updated to match, so the review page, rollback and the commit all agree. The commit message names the set it is stacked on. If a replay fails or the check finds a problem, apply fails with a conflict and the set stays `approved`. Why: change sets made from the same `HEAD` (for example `fix` and `document` on the same measure) would otherwise produce sibling branches that conflict when merged. Stacked, the newest branch holds every applied set, and merging it alone brings them all in. Found in the Phase 1 Desktop check on 2026-10-28.
- **rollback:** a new commit on the same branch that restores every item's `before`. A set that another applied set is stacked on can't be rolled back until that later set is rolled back (rolling back the earlier branch alone would leave its edits in the later one).
- Every transition writes an AuditLog row.

## TMDL support (Phase 0 scope)

- **Reader** (`connectors/powerbi/tmdl.py`) parses `definition/tables/*.tmdl` and `relationships.tmdl` into tables, columns, measures and relationships. It captures names (quoted or unquoted), `///` descriptions, measure expressions (single-line, multi-line and ```` ``` ````-fenced), and properties like `dataType`, `formatString`, `isHidden`, `summarizeBy`, `displayFolder`, `crossFilteringBehavior`, `fromColumn` and `toColumn`, plus declaration line numbers.
- **Editor:** `set_description(text, object, description)` replaces the contiguous `///` block directly above the declaration (or inserts one). It keeps the declaration's indentation, the file's newline style (LF or CRLF) and any BOM. Every other byte stays the same.
- **Golden tests:** edits on the fixtures must match the golden files byte for byte.

## Audit rules (Phase 0 set; Phase 1 ports the BPA list)

| Rule id | Severity | Finds |
|---|---|---|
| `measure-missing-description` | warning | Measures with no `///` description |
| `use-divide-function` | warning | A `/` division operator in a measure expression (outside strings and comments) |
| `avoid-floating-point` | warning | Columns with `dataType: double` |
| `avoid-bidirectional-relationship` | warning | `crossFilteringBehavior: bothDirections` |

## Agent runtime (`agents/runtime.py`, ADR 0002)

- `AgentRunner(session, client, settings).run(run, system=..., user_prompt=..., registry=..., ctx=...)`: our own Messages API loop (`client.beta.messages.create`, so the refusal-fallback beta can be sent).
- Each model call and each tool call is an AgentStep. Usage and cost are summed onto the AgentRun.
- Tool registry entries have a name, description, JSON schema (`strict`), capability, timeout and cost class (`cheap` or `expensive`), plus a Python handler. The timeout is declared now and enforced once the first network-bound tool exists; every Phase 0 tool is local and fast.
- Tool results that carry customer text are wrapped in `<customer_data>…</customer_data>`.
- Stop handling: `end_turn` → done; `tool_use` → run the tools and continue; `max_tokens` → fail the run; `refusal` → status `refused`; `pause_turn` → continue; more than `max_steps` → fail the run.
- Tests and `--offline` mode inject a `ScriptedClient` that returns pre-written responses.

### First agent: `document-measures`

- Tools: `list_measures` (READ), `get_measure` (READ), `propose_description` (PROPOSE).
- Instructions come from the skill file `agents/skills/powerbi/document-measures.md`.
- Output: one change set in `proposed` status, with each item linked to its `measure-missing-description` finding.

## Local web UI (`web/`)

- Server-rendered pages: dashboard, run detail with steps, change set detail with a coloured diff and actions, findings.
- Binds to `127.0.0.1` only. Requests with a `Host` other than `127.0.0.1:<port>` or `localhost:<port>` are refused. A POST must carry the per-process CSRF token, and a present `Origin` must match.
- Actions are plain HTML forms that POST, then redirect with 303.

## CLI (`datawright …`)

`connect`, `crawl`, `audit`, `document [--offline]`, `demo [--offline]`, `serve`, `evals run --set <name>`.

## Evals

- Datasets live in `evals/datasets/<set>.yaml` and thresholds in `evals/thresholds.yaml`.
- `audit-rules-v0` is deterministic. It runs the auditor on the fixtures and expects the exact planted findings.
- `describe-v0` needs a model. It runs the documenter on fixture measures and scores the descriptions with deterministic checks: present, ≤ 300 characters, no raw DAX, and names at least one referenced column or measure. It's skipped when no credentials are configured.
- The harness writes EvalResult rows and exits non-zero if a set falls below its threshold.

## Fixtures

`fixtures/pbip/retail-clean` and `fixtures/pbip/retail-issues` are hand-written in the PBIP/TMDL format, semantic model only. They have **not yet been opened in Power BI Desktop**, because Desktop isn't installed on the dev machine. Phase 1 exit requires opening them in Desktop, or replacing them with Desktop-saved copies.

The SQL, dbt, Airflow and notebook fixtures from the original plan move to the phases that need them.

## Phase 0 exit criteria

1. `datawright demo` reads `retail-issues`, records findings, runs the documenter (live, or with `--offline`), and leaves one change set in `proposed`.
2. In the UI, the change set's diff is visible and can be approved and applied. The result is a commit on `datawright/cs-<id>`, and the user's working tree is untouched. Rollback works.
3. The run's steps are visible in the UI.
4. Lint, types and tests pass locally (`ruff`, `mypy`, `pytest`), and the same commands are in `.github/workflows/ci.yml`. `audit-rules-v0` passes at its threshold.
