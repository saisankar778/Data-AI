# Status and handoff

Last updated 2026-10-28. Read this first when continuing the work, on any machine and with any AI assistant. Then read `CLAUDE.md` for the rules.

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

Checks: 139 tests, ruff, mypy, and the deterministic evals `audit-rules-v1` (45 cases) and `refresh-doctor-v0` (11 cases) pass.

## Verified, and not yet verified

- Verified on 2026-10-27 in Power BI Desktop (Oct 2026, on the user's laptop): the hand-written `retail-report` project **opened**, with all four tables listed. Refresh failed only because the sample data didn't exist yet; `datawright sample` now provides it. The report files were then switched to exactly the formats that opened.
- **Not yet verified:**
  1. Refresh of a `datawright sample` copy in Desktop (data should load).
  2. **Desktop accepting Datawright's own edits.** Run `fix` and `document --offline` on a sample, apply, check out the `datawright/cs-N` branch, then open the project in Desktop. This is the Phase 1 exit check.
  3. The model evals (`describe-v1`, `ask-v0`) with a real API key.
  4. A live MCP session from Claude Desktop.

## Next steps

**For the person (needs the laptop with Power BI Desktop):**

1. `git pull`, then `.\.venv\Scripts\python -m pip install -e ".[dev]"` (to pick up new commands).
2. `datawright sample retail-report C:\PowerBI\retail-report`, open `Retail.pbip`, click Refresh. Report back: did data load, and do the visuals show?
3. Verification 2 above (fix → apply → open in Desktop). Screenshot any error.
4. With the API key in `.env`: `datawright evals run --set describe-v1` and `datawright evals run --set ask-v0`.
5. Phase A interviews (`docs/validation/phase-a.md`): 3 design partners before more features.

**For the AI assistant, in order:**

1. Fix anything verifications 1–2 reveal. Desktop may rewrite TMDL into its own canonical form; if it does, compare and adjust the generator and editor, keeping edits byte-minimal.
2. **Copilot "Prep data for AI" pack (Phase 1b).** First have the person set AI instructions, a verified answer and synonyms in Desktop on a sample, save, and commit, so the PBIP files show exactly where Desktop stores them. Then generate them as change sets. Don't guess the format.
3. Model-judge scoring for `describe-v1` (rubric graded by Claude), once a key is available.
4. Phase 2 (`docs/PLAN.md`): Windows desktop agent with the Power BI Modeling MCP for live DAX; Ask with numbers; Narratives; template-based PBIR page builder (the PBIR visual format in `fixtures/generate.py` is a starting point); billing.
5. Phase 3: cloud workspace (Postgres, sign-in, second-person approval), tenant governance scan, SQL sources.

## Working notes

- Python 3.11+ in `.venv`; run tests with `.venv\Scripts\python -m pytest`.
- Regenerate samples with `python fixtures/generate.py` (TMDL is byte-compared by tests and golden files).
- Write code containing backslash escapes (`\n`, `\b`, `\u...`) with an editor or file-write tool, not through shell heredocs. On the original dev machine those escapes got mangled.
- Keep the repository out of OneDrive-synced folders if possible: syncing `.venv` and `.git` is slow and can corrupt Git's lock files.
