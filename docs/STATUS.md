# Status and handoff

Last updated 2026-10-29. Read this first when continuing the work, on any machine and with any AI assistant. Then read `CLAUDE.md` for the rules.

## Where things are

- Code: https://github.com/saisankar778/Data-AI (branch `main`). Work on `main`, and commit and push after each finished feature.
- The plan is `docs/PLAN.md`, the review that reshaped it is `docs/REVIEW.md`, decisions are in `docs/adr/`, and module specs are in `docs/specs/`.

## What's built

| Area | Commands | Notes |
|---|---|---|
| Health check (38 rules: Best Practice Analyzer port, Power Query refresh risks, AI readiness, unused objects) | `check` (`--format json/sarif/github`, `--fail-on`, `--min-score`) | Works on PBIP folders and `.pbix` |
| Automatic fixes for 8 rules + how-to-fix advice for all rules | `fix [--dry-run]` | Proposed as a reviewed change set |
| Descriptions drafted by Claude (tables, columns, measures) | `document` | Needs the API key, or `--offline` |
| Questions about a model | `ask` | Needs the API key, or `--offline` |
| Semantic diff (changes, broken DAX references, broken visuals, health delta) | `diff` | Also shown on the change-set review page |
| Refresh doctor (15 refresh-error patterns located in the model) | `diagnose` (`--explain` uses Claude) | Also a UI page and MCP tool |
| Report usage (which fields visuals use: PBIR, classic layout, inside `.pbix`) | automatic | Drives the unused-object rules and broken-visual warnings |
| Review UI (edit-by-edit accept/reject/edit, apply as a Git branch, roll back) | `serve` | http://127.0.0.1:8765 |
| MCP server for Claude Desktop / Claude Code (read + propose only) | `mcp` | Setup in the README |
| CI gate and PR comment | `examples/github-actions/power-bi-checks.yml` | |
| Sample projects with data for Power BI Desktop | `sample <name> <folder>` | `retail-clean`, `retail-issues`, `retail-report` |

Checks: 144 tests, ruff, mypy, and the deterministic evals `audit-rules-v1` (45 cases) and `refresh-doctor-v0` (11 cases) pass.

## Verified, and not yet verified

- Verified on 2026-10-27 in Power BI Desktop (Oct 2026, on the user's laptop): the hand-written `retail-report` project **opened**, with all four tables listed. Refresh failed only because the sample data didn't exist yet; `datawright sample` now provides it. The report files were then switched to exactly the formats that opened.
- Verified on 2026-10-28 in Desktop 2.159.883 (Oct 2026): a `datawright sample retail-report` copy **refreshes** (Sales 156 rows, Product 12, Customer 10, Calendar 1,096; Total Sales 3,186,550; Margin 38%) and its **report shows the generated visuals** (KPI card, bar chart, line chart, slicer). The first attempt showed a blank report: with a `report.json` lacking `themeCollection`, Desktop silently dropped all pages and visuals. `report.json`, `pages.json` and `page.json` are now written exactly as Desktop saves them.
- What Desktop does to TMDL when it saves (seen on 2026-10-28, diff of a sample before/after save): CRLF line endings and a trailing blank line; `compatibilityLevel` 1567 → 1606; property order changes (`lineageTag` before `dataCategory`, `formatString` after `lineageTag`, `isHidden` before `lineageTag`); single-line expressions lose their ``` fences; adds `annotation PBI_ResultType`, `PBI_ProTooling = ["DevMode"]`, `UnderlyingDateTimeDataType = Date`; writes `.platform` files, `diagramLayout.json`, and `.pbi/` (cache.abf, localSettings, editorSettings), plus a `.gitignore`. Our generator's TMDL is still the pre-Desktop form; the editor must keep working on both.
- **Phase 1 exit check passed on 2026-10-28** (Desktop 2.159.883): on `C:\PowerBI\retail-issues`, change set #1 (17 fixes) and #2 (10 offline descriptions) were applied as `datawright/cs-1` and `datawright/cs-2`, merged on a test branch, and opened in Desktop. Desktop loaded every edit (descriptions, `DIVIDE` rewrite, hidden foreign keys, key columns, `dataCategory: City`), refreshed all 5 tables without errors (Sales 156 rows, Margin 38%), and changed no files on open.
- **Found in that check:** two change sets made from the same commit conflicted when merged: #1 rewrote `measure 'Margin %'` and #2 added its `///` description on the line above. **Fixed 2026-10-29:** apply now *stacks* a change set on the newest applied, unmerged `datawright/cs-*` branch of the connection (intents replayed and re-checked there), so the newest branch holds every applied set and merging it alone brings them all in. The review page says "Stacked on change set #N", and an earlier set can't be rolled back while a later one sits on it. Spec: `docs/specs/core.md`, "stacking".
- **Not yet verified:**
  1. **Desktop accepting Datawright's own edits.** Done (see above); kept here for the record. Run `fix` and `document --offline` on a sample, apply, check out the `datawright/cs-N` branch, then open the project in Desktop. This is the Phase 1 exit check.
  2. The model evals (`describe-v1`, `ask-v0`) with a real API key.
  3. A live MCP session from Claude Desktop.
  4. The Details page visuals (table, map) and the visualContainer schema 1.0.0 we still write (Desktop accepted it on the Overview page).

## Next steps

**For the person (needs the laptop with Power BI Desktop):**

1. `git pull`, then `.\.venv\Scripts\python -m pip install -e ".[dev]"` (to pick up new commands).
2. Done 2026-10-28: sample refresh and report visuals work.
3. Done 2026-10-28: fix → apply → open in Desktop passed.
4. With the API key in `.env`: `datawright evals run --set describe-v1` and `datawright evals run --set ask-v0`.
5. Phase A interviews (`docs/validation/phase-a.md`): 3 design partners before more features.

**For the AI assistant, in order:**

1. Fix anything verification 1 reveals. Done 2026-10-28: description, property and expression edits stay one-line and CRLF-preserving on a file Desktop saved (`tests/golden/desktop-saved/Sales.tmdl`, `test_edits_on_a_file_power_bi_desktop_saved`). Watch: the laptop has global `core.autocrlf=true`, so checking out a `datawright/cs-*` branch turns LF files into CRLF in the working tree (harmless for Desktop, but `git status` may show them modified).
2. Done 2026-10-29: change sets for the same connection stack instead of conflicting.
3. **Copilot "Prep data for AI" pack (Phase 1b). Blocked:** on 2026-10-29 the person's Copilot sign-in failed in Desktop (Copilot needs a work account on a Copilot-enabled capacity: Fabric F2+ or Premium; a personal or free account can't author it). Known from Microsoft Learn: PBIP stores it in a `Copilot/` folder inside the `*.SemanticModel` folder, with `Instructions/instructions.md`, a `version.json` and a `VerifiedAnswers/` folder; the docs don't show the file contents, and older models keep synonyms in the Q&A linguistic schema (LSDL) with a migration prompt. Still capture a real save before generating anything. Don't guess the format.
4. Model-judge scoring for `describe-v1` (rubric graded by Claude), once a key is available.
5. Phase 2 (`docs/PLAN.md`): Windows desktop agent with the Power BI Modeling MCP for live DAX; Ask with numbers; Narratives; template-based PBIR page builder (the PBIR visual format in `fixtures/generate.py` is a starting point); billing.
6. Phase 3: cloud workspace (Postgres, sign-in, second-person approval), tenant governance scan, SQL sources.

## Working notes

- Python 3.11+ in `.venv`; run tests with `.venv\Scripts\python -m pytest`.
- Regenerate samples with `python fixtures/generate.py` (TMDL is byte-compared by tests and golden files).
- Write code containing backslash escapes (`\n`, `\b`, `\u...`) with an editor or file-write tool, not through shell heredocs. On the original dev machine those escapes got mangled.
- Keep the repository out of OneDrive-synced folders if possible: syncing `.venv` and `.git` is slow and can corrupt Git's lock files.
