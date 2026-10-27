# data ai: Datawright (working name)

Datawright helps Power BI teams first. It audits, fixes and documents semantic models and makes them AI-ready. Every change it makes is a **change set a person reviews**, and it's applied as a commit on a new Git branch, without touching your working copy. It runs locally on your Windows machine. Model files stay on disk, and only metadata (names, types, DAX) goes to Claude.

**Status:** Phase 0 (foundation) is built and tested. Phase A (validation with real users) is next, and it's the team's job: see [docs/validation/phase-a.md](docs/validation/phase-a.md).

## Try it (no API key needed)

```bash
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/datawright demo --offline   # copies a sample model into a Git repo, audits it, proposes 3 descriptions
.venv/Scripts/datawright serve            # open http://127.0.0.1:8765, review, approve, apply, roll back
```

With a Claude API key (`set ANTHROPIC_API_KEY=...`), drop `--offline` and Claude writes the descriptions.

On your own model: `datawright connect mymodel <folder with *.SemanticModel> --capability apply`, then `datawright audit mymodel` and `datawright document mymodel`.

## Docs

| File | What it is |
|---|---|
| [docs/REVIEW.md](docs/REVIEW.md) | Review of the original plan: verdict, problems, ideas, phase-by-phase roadmap |
| [docs/PLAN.md](docs/PLAN.md) | The product plan, revised for the agreed decisions |
| [docs/adr/](docs/adr/) | Decisions: Power BI first and local-first (0001), agent runtime and model (0002), local stack (0003) |
| [docs/specs/core.md](docs/specs/core.md) | Phase 0 spec: entities, change set lifecycle, TMDL editing, agent loop, UI security |
| [docs/validation/phase-a.md](docs/validation/phase-a.md) | Interview guide, concierge test and tracker for the validation gate |
| [CLAUDE.md](CLAUDE.md) | Rules and commands for Claude Code in this repo |

## Checks

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format --check . && .venv/Scripts/python -m mypy
.venv/Scripts/datawright evals run --set audit-rules-v1
```
