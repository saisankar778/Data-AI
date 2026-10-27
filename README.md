# Datawright (working name)

**An AI "model doctor" for Power BI.** Datawright audits a semantic model, explains every problem, writes the missing documentation, answers questions about the model, and proposes fixes. Every change is a **change set that a person reviews edit by edit**. Approved changes become a commit on a new Git branch; your working copy is never touched, and every change can be rolled back.

It runs **locally on your Windows laptop** next to Power BI Desktop. Model files stay on disk, and only metadata (names, types, DAX) ever goes to Claude, never table data.

## What it does today

| Command | What you get |
|---|---|
| `datawright check <file.pbix or PBIP folder>` | Health score, AI-readiness and top problems in seconds. 35 rules: Microsoft's Best Practice Analyzer rules, Power Query refresh risks, AI readiness. No setup, no API key |
| `datawright connect` / `track` | Connect a `.pbix` (read only) or a PBIP folder in Git (read / propose / apply) |
| `datawright document <connection>` | Claude drafts descriptions for undocumented tables, columns and measures, as a change set for review |
| `datawright ask <connection> "question"` | Answers questions about the model ("which measure is margin?") and cites the objects it used |
| `datawright guide <connection>` | A model guide (data dictionary) in Markdown; the UI version prints to PDF |
| `datawright serve` | Local review UI at http://127.0.0.1:8765: health, findings, change sets, edit-by-edit review, apply, roll back |
| `datawright mcp` | MCP server, so Claude Desktop, Claude Code or VS Code can use Datawright with no API key. It can only propose; a person approves |

## Install on any Windows laptop

You need **Python 3.11+** ([python.org](https://www.python.org/downloads/), tick *Add python.exe to PATH*) and **Git** ([git-scm.com](https://git-scm.com/download/win)).

```powershell
git clone https://github.com/saisankar778/Data-AI.git
cd Data-AI
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The script creates `.venv`, installs Datawright, creates `.env` from `.env.example`, and runs a first health check.

To set up by hand instead:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
copy .env.example .env
```

On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/...` (`check`, `document`, `ask`, the UI and MCP all work there; Power BI Desktop itself is Windows-only).

### Add your Claude API key

Open `.env` and set `ANTHROPIC_API_KEY=...`. `.env` is git-ignored, so the key never leaves your laptop. You only need it for `document`, `ask` and the model evals. Without a key, add `--offline` to those commands to run them with placeholder drafts.

## Try it

```powershell
.\.venv\Scripts\datawright check ".\fixtures\pbip\retail-issues"   # a model with planted problems
.\.venv\Scripts\datawright demo --offline                          # audit + draft descriptions + change set
.\.venv\Scripts\datawright serve                                   # review at http://127.0.0.1:8765
```

On your own model:

```powershell
.\.venv\Scripts\datawright check "C:\Reports\Sales.pbix"
# To let Datawright propose and apply changes, save the report as PBIP in Power BI Desktop
# (File > Save as > Power BI Project), then:
.\.venv\Scripts\datawright track "C:\Reports\Sales"
.\.venv\Scripts\datawright connect sales "C:\Reports\Sales" --capability apply
.\.venv\Scripts\datawright document sales
```

## Use it from Claude Desktop or Claude Code (MCP)

Claude Desktop: add this to `%APPDATA%\Claude\claude_desktop_config.json`, with your own path, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "datawright": {
      "command": "C:\\path\\to\\Data-AI\\.venv\\Scripts\\datawright.exe",
      "args": ["mcp"]
    }
  }
}
```

Claude Code: `claude mcp add datawright -- C:\path\to\Data-AI\.venv\Scripts\datawright.exe mcp`

Then ask, for example, *"Use Datawright to check the health of my sales model and document the undocumented measures."* Claude can read and propose; you approve in the review UI.

## Docs

| File | What it is |
|---|---|
| [docs/REVIEW.md](docs/REVIEW.md) | Review of the original plan: verdict, problems, ideas, roadmap |
| [docs/PLAN.md](docs/PLAN.md) | The product plan, revised (Power BI first, local-first) |
| [docs/adr/](docs/adr/) | Decisions: scope (0001), agent runtime (0002), local stack (0003), `.pbix` + MCP (0004) |
| [docs/specs/](docs/specs/) | `core.md` (Phase 0 platform), `powerbi.md` (Phase 1 Model Doctor) |
| [docs/validation/phase-a.md](docs/validation/phase-a.md) | Interview guide and concierge test for finding design partners |
| [CLAUDE.md](CLAUDE.md) | Rules and commands for Claude Code in this repo |
| [NOTICE](NOTICE) | Third-party licences (Microsoft BPA rules, Power BI sample) |

## Checks

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check . ; .\.venv\Scripts\python -m ruff format --check . ; .\.venv\Scripts\python -m mypy
.\.venv\Scripts\datawright evals run --set audit-rules-v1
.\.venv\Scripts\datawright evals run --set describe-v1    # needs the API key (or --offline)
.\.venv\Scripts\datawright evals run --set ask-v0         # needs the API key (or --offline)
```
