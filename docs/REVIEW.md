# Review of the Data Workbench (Datawright) build plan

Reviewed 26 Oct 2026, against `docs/PLAN.md` (plan dated 25 Oct 2026).

## 1. Verdict

**Yes, this is worth building, but not in the form the plan describes.**

The problem is real. Data teams lose hours every week to ad hoc questions, undocumented models and broken pipelines. The plan's core ideas are also right: changes go out as diffs, a person approves every write, connections are read-only by default, and evals block a merge when quality drops. Those ideas are what would make this a product people trust rather than another chatbot.

What needs to change is scope and focus. The plan describes 9 modules, about 20 tool integrations, 6 user surfaces, 3 programming languages and 3 user roles, all for a team of 2 to 3 people over 31 weeks. Even its "MVP" (Phases 0 to 2) is 14 weeks of building before the first pilot user sees anything. The plan's own risk table rates "scope too big for a small team" as High likelihood and High impact, but the phases don't act on it.

"A product everyone needs" is the weakest idea in the plan. A product built for everyone usually has no first customer. Pick one group with sharp pain and win them first. The cross-tool platform can grow out of that.

**My recommendation:** start with **Power BI teams**, especially teams that can't use Copilot. Give them something useful in about 6 weeks, and add a validation gate before writing any platform code. The full reasoning and a revised phase plan are below.

Build it if both of these are true:

1. Within two weeks you can find **3 design partners**: real Power BI developers or analysts who will try it on their own models.
2. 2 to 3 people can give it steady time for about 6 months alongside the SkyWorks hardware work (GCS, AirTaxi, NIDAR and the rest).

## 2. What the plan gets right (keep all of this)

- **Change sets as the centre of the product.** Agents never write to a customer system directly. This is the trust story, and it is something a plain chat tool doesn't offer.
- **Security defaults from day one:** read-only connections, a SQL guard built on sqlglot, secrets never placed in prompts, and text from data treated as data, never as instructions.
- **Evals that gate merges**, using internal eval sets. The success metric is "accepted output" and "time saved", not chat volume.
- **Connect, don't replace.** It reuses official MCP servers and builds adapters only where there is a gap.
- **Exit criteria for every phase.**
- **A specific Power BI angle.** This is where the real gap is (see 3.2).

## 3. Problems to fix

### 3.1 Scope: too wide to finish, too wide to sell

| Count in the plan | Number |
|---|---|
| Product modules | 9 |
| Tool integrations | ~20 |
| User surfaces | 6 (web, CLI, VS Code, MCP server, Slack/Teams, Windows desktop agent) |
| Languages | 3 (Python, TypeScript, .NET) |
| Database entities in Phase 0 | 16 |
| Weeks before the first pilot user | 14 |

Each integration is a maintenance cost that never goes away, because APIs, MCP servers and file formats keep changing. A small team should run 2 or 3 integrations really well, not 20 at a basic level.

### 3.2 The first module (Ask) is in the most crowded part of the market

Phase 1 builds text-to-SQL question answering, and that space is full. Databricks Genie, Snowflake Cortex Analyst and Hex already cover it, along with dbt's Analyst Agent (private beta) and open-source tools. Any user with Claude and a database MCP server can also do it themselves. Phase 3 has the same problem: dbt generation and pipeline diagnosis overlap with dbt Labs' own **Developer Agent** (in preview since May 2026) and their planned **Observability Agent**.

The gap the plan already noticed is real, and it should become the starting point:

- **Copilot in Power BI requires F2+ Fabric or P1+ Premium capacity.** A Pro or PPU licence alone is not enough. Many teams, especially SMBs, consultancies and Indian companies, are on Pro only and get no AI help at all.
- **Teams that do have Fabric still need to "prep data for AI"** before Copilot gives good answers. Microsoft's *Prep data for AI* feature means writing AI instructions, verified answers and an AI data schema, plus good descriptions. Today people do this by hand. Datawright can do it for them, which means working with Microsoft's direction instead of competing with it.

### 3.3 The real competitor isn't named: Claude Code plus free MCP servers

Microsoft's Power BI Modeling MCP server (public preview) can already edit PBIP/TMDL files on disk, and the dbt MCP server is free. A skilled user can point Claude Code or Claude Desktop at them today and get a lot of M2, M3 and M5 for free. The plan must answer "why not just do that?" and build towards that answer. Here is what a single chat session can't give a team:

- shared team context and **verified queries** that build up over time
- **review and approval** for people who don't use Git, with history and rollback
- **rule-based audits** with a health score over time, not one-off chat answers
- **proof of quality** (eval results per release)
- a **no-code path** for the "application users" persona

### 3.4 There is no business plan

The plan has no target customer, price, sales channel, named pilot users or point at which you'd stop. Billing only shows up in Phase 5, around week 25. These need to be decided before Phase 0, because they change what gets built first.

### 3.5 Phase 0 builds enterprise plumbing before anything useful exists

Phase 0 includes OIDC login with roles, Celery and Redis, a Docker sandbox, OpenTelemetry, a .NET worker stub and all 16 entities. None of that is needed for the first pilot. Revised Phase 0 in section 5 shows what to defer and when it comes back.

### 3.6 Practical gaps in the Power BI part

- **Most users have `.pbix` files, not PBIP.** The plan only crawls PBIP folders. Add `.pbix` intake: the open-source `pbixray` Python library reads measures, M code, schema and relationships from a `.pbix` on any OS. Also add a guided "Save as PBIP" step for when they want write-back.
- **Don't write the audit rules from scratch.** Microsoft publishes the Best Practice Analyzer rule set used by Tabular Editor, and PBI Inspector has report-level rules (check both licences). Then the LLM's job is to explain and fix the findings, not to find them, which is more reliable and much cheaper.
- **Free-form PBIR page generation is the riskiest item in the MVP.** Visual JSON is large and fragile. Limit it to a small library of tested visual templates (card, bar, line, table, matrix, slicer). The LLM picks the template, fields, measures and layout; it never writes raw visual JSON.
- **The .NET `pbi-worker` may not be needed for the MVP.** The Modeling MCP server can now open PBIP files on disk. Check whether it covers TMDL load/save/validate for you, and whether it runs on Linux. If it does, use it. If not, keep the TOM worker. Don't build both.

### 3.7 Technical corrections

- **`claude-haiku-5-5` does not exist.** The current small model is `claude-haiku-4-5` ($1 / $5 per million input/output tokens, 200K context). Current list prices: `claude-sonnet-5-5` $2 / $10 and `claude-opus-5-5` $4 / $20, both with 1M context.
- **Measure before building a three-model router.** Prompt caches are per model, so routing a task across models loses cache reuse. Opus 5.5 or Sonnet 5.5 at a lower `effort` setting is often cheaper per accepted task than a Haiku→Sonnet→Opus cascade. Start with one model, track cost per accepted task, and only split once the numbers show a saving.
- **Agent runtime choice:**
  - The **Claude Agent SDK** is the Claude Code harness, with built-in file and bash tools, that you host yourself. It fits the **local/desktop** side well, for example an agent working on PBIP files on a user's Windows machine.
  - For the **multi-tenant server**, where every tool is your own connector and every write needs an approval gate, the **Messages API tool runner** in the normal `anthropic` SDK fits better. You define every tool, and its per-turn hooks give you approval gates, logging and error handling.
  - **Managed Agents** (Anthropic hosts the loop and a sandbox, with scheduled runs) could later replace your own Docker sandbox and Celery schedules. Customer data would then run on Anthropic's infrastructure, though, so self-hosted customers can't use it.

  Record this as `docs/adr/0002-agent-runtime.md`.
- **Three languages from the start is too many.** For the MVP, use Python plus one small web UI. Add the .NET worker, the VS Code extension and the CLI only when a phase needs them.

### 3.8 Evals: good, but in the wrong order

Spider 2.0 and ELT-Bench take real time and warehouse spend, and they don't tell you whether your pilot users will accept the output. Build the internal sets first (PBI-Bench, then SQL-Internal). Run the public benchmarks later, mostly for marketing.

### 3.9 Security: two additions

- If the first customers are in India, check your obligations under the **DPDP Act** and write a short data processing note that says what goes to the model provider, what doesn't, and where it runs.
- A `.pbix` file **contains the data itself**, not just the model. Any upload feature must parse metadata only and delete the file straight away, and it must say so on screen.

## 4. Ideas to make it better

1. **Wedge: "Make your Power BI model AI-ready."** Audit, fix, document, then generate the Copilot *Prep data for AI* content (descriptions, synonyms, AI instructions, suggested verified answers). One product, two buyers:
   - Pro-only teams: Datawright *is* their AI.
   - Fabric teams: their Copilot gives poor answers on messy models.

   First check how the *Prep data for AI* items are stored in PBIP before promising that part.
2. **A free Model Health Check to find users.** Drop in a `.pbix` or PBIP and get a score and the top 10 issues in under a minute, with no signup and metadata only. It's easy to share in the Power BI community and on LinkedIn, and every run is a lead.
3. **First customers: Power BI freelancers and consultancies.** They handle many client models, bill by the hour, buy fast and need no enterprise security review. India has a large Power BI services market. Move on to SMB data teams next, and enterprise (SSO, Helm) last.
4. **MCP server first, not in Phase 4.** Users already live in Claude Desktop, Claude Code and VS Code Copilot. Ship `datawright-mcp` in the first build phase so they can use Datawright from those tools on day one. The web app then only has to do what chat can't: show diffs, approve, track history and hold the catalog.
5. **Local-first for the MVP.** Power BI Desktop runs only on Windows, and PBIP files live on disk. A local app (Python backend with a web UI on localhost) keeps customer data on the laptop and sends only metadata to the model. It has no hosting cost and needs no security review to start. Team features (shared context, approval by a second person) come with the cloud version in revised Phase 3. The team has already shipped Electron apps (the GCS), so packaging is familiar.
6. **Move Narratives earlier.** A weekly "what changed and why" email built from the Power BI model goes to **managers, who hold the budget**. It reuses the same model and DAX, and it makes the product's value visible every week.
7. **Answer questions from the Power BI semantic model (DAX) before raw SQL.** The model already has relationships, measures and business names, so answers are more accurate than raw text-to-SQL, and you aren't competing with every SQL bot.
8. **Excel→Power BI migration as the second paid wedge.** The Excel Bridge module is a very common, hand-done consulting job.
9. **Learn from rejections automatically.** Every rejected change set becomes an eval case. Each workspace can switch off audit rules it doesn't care about.
10. **Clear go/no-go gates with numbers.** See section 6.

## 5. Revised roadmap, phase by phase

| | Original plan | Revised |
|---|---|---|
| First thing users see | Week 14 (Context Hub + Ask + Power BI Studio) | Week 9 (Power BI model doctor + free health check) |
| First paying customer | Not planned | Target week 14 |
| Validation before code | None | Weeks 0–2 |
| Integrations in the MVP | ~10 | 2 (Power BI files, Power BI Desktop) |
| Surfaces in the MVP | Web, CLI, desktop agent | MCP server + one review web page |

### Phase A: Validate (weeks 0–2, no platform code)

- Do 15 short interviews with Power BI developers, analysts and consultants (college alumni in analytics roles, LinkedIn, the Power BI community).
- Run a **concierge test**: with permission, take 3 real models and audit and document them by hand using Claude Code, the Power BI Modeling MCP and the BPA rules. Deliver the report, then ask what was useful and what they would pay for it.
- Record for each person: `.pbix` or PBIP, Pro or Fabric, Git or no Git, team size, current tools.
- Build the two fixture PBIP projects from the plan (one clean, one with planted problems).

**Exit:** 3 design partners committed to a pilot on real models; at least 2 say they'd pay at a named price; a one-page description of the target customer written down.

### Phase 0: Thin foundation (weeks 2–4)

- **Build:** monorepo (Python with uv, plus one small web app), Postgres, and only the entities needed now (Workspace, User, Asset, AgentRun, AgentStep, ChangeSet, ChangeItem, Finding, EvalCase, EvalResult, AuditLog). Also build the ChangeSet service with Git branch and diff, the agent runtime on the tool runner with a tool registry that has capability levels (read / propose / apply), steps stored in the DB as the trace, an eval harness skeleton, and CI.
- **Defer until needed:** OIDC/SSO (Phase 3), Celery and Redis (Phase 3), the Docker sandbox (when generated code first runs), OpenTelemetry export, the SQL guard (with the first SQL connector), MySQL, SQL Server and the warehouses.

**Exit:** a demo run reads a fixture PBIP, creates one Finding and one ChangeSet (a measure description), shows it as a diff, gets approved, and is applied to a Git branch. The steps are visible in the UI, and CI is green.

### Phase 1: Power BI Model Doctor (weeks 4–9, first pilot release)

- **Intake:** PBIP/TMDL folders, plus `.pbix` read-only through `pbixray` (metadata only).
- **Auditor:** the BPA rule set plus your own rules. Each finding has a severity, a plain-language explanation and a proposed TMDL fix.
- **Documenter:** descriptions, synonyms and display folders as a TMDL change set, plus a readable model guide (Markdown/PDF).
- **AI-ready pack:** AI instructions and suggested verified answers, if PBIP storage allows it.
- **Surfaces:** `datawright-mcp` (tools: `audit_model`, `document_model`, `propose_fix`, `list_findings`, `apply_change_set`) and one web page to review, approve or reject with a comment.
- **Free Health Check** page (metadata only).
- **PBI-Bench v0:** 30–40 cases.

**Exit:** TMDL round-trip golden tests pass, and an applied model opens in Power BI Desktop without errors. All 3 pilots use it on their own models at least twice in two weeks. The description acceptance rate is measured, with a starting target of at least 60% accepted without edits (agree the final target with the pilots).

### Phase 2: DAX, Ask and Narratives (weeks 9–14, first paid version)

- **Windows desktop agent** (Agent SDK, or plain Python plus the Modeling MCP) for live DAX validation in Power BI Desktop.
- **DAX helper:** write, explain and optimise measures, and run each one before proposing it.
- **Ask over the semantic model:** a question becomes a DAX query, and the answer always shows the query. Includes "save as verified query".
- **Narratives v1:** a weekly "what changed" report for chosen measures, as Markdown or PDF, sent by email.
- **Report page builder:** PBIR from 5–6 tested visual templates only.
- **Start charging.**

**Exit:** a PBI-Bench baseline of about 100 cases; generated pages open in Desktop without errors across the test set; at least one paying customer.

### Phase 3: Team, governance and SQL (weeks 14–20)

- A cloud workspace for several users: roles, approval by a second person, and a shared Context Hub (verified queries, glossary).
- OIDC login, and a job queue for scheduled crawls and narratives (brought back from Phase 0).
- **Tenant governance scan** through the Admin APIs, read-only. This one is sold to BI admins.
- Crawlers for Postgres plus **one** warehouse that the pilots actually use, the SQL guard, and Ask on SQL.

**Exit:** the plan's M1 and M2 acceptance criteria pass, and one team of at least 3 people works in a shared workspace.

### Phase 4 and later: driven by demand

Choose from: Excel Bridge, Pipeline Copilot (dbt/Airflow, minding the overlap with dbt's own agents), Prep & Quality, Notebook Copilot, Request Desk, Tableau/Looker, and enterprise features (SAML, Helm, admin console).
**Rule:** build a module only when at least 3 paying customers ask for it.

## 6. Go/no-go gates

| When | Check | If it fails |
|---|---|---|
| Week 2 | 3 design partners committed | Don't build yet. Test the second wedge (Excel→Power BI migration) with the same interview method |
| Week 9 | Pilots come back on their own; acceptance rate is measured and rising | Stop adding features and fix quality until it improves |
| Week 14 | At least one paying customer | Stop or change direction. Don't build Phase 3 on hope |

## 7. Questions to answer before Phase 0

1. Who on the team has worked in a data or BI role? Which 3 people could you show this to next week?
2. Do you agree with **Power BI first**, or is there a reason to start with SQL Ask as the plan says?
3. **Local-first** (runs on the user's Windows machine) or **cloud-first** for the MVP?
4. How many hours a week can each person give, alongside SkyWorks hardware work?
5. Name check: search "Datawright" for existing companies and trademarks before buying a domain.

## 8. Changes to make in `PLAN.md` once you agree

- Replace `claude-haiku-5-5` with `claude-haiku-4-5`. Start with one model and add routing only after measuring.
- Change the agent runtime row: tool runner on the server, Agent SDK for the desktop agent, Managed Agents as a later option (ADR 0002).
- Replace the Build phases table with section 5 of this review, and move `datawright-mcp` from Phase 4 to Phase 1.
- Add `.pbix` intake (`pbixray`), the BPA rule reuse and template-based PBIR to M3.
- Add a short "Business" section: target customer, pricing to test, channel and the gates from section 6.
- Update `CLAUDE.md` and the per-phase prompts to match.

## Sources checked for this review

- [Power BI Modeling MCP server (Microsoft, public preview; works with PBIP files on disk)](https://github.com/microsoft/powerbi-modeling-mcp)
- [Copilot in Power BI needs F2+ or P1+ capacity; Pro/PPU alone is not enough](https://book.st-hakky.com/en/data-analysis/power-bi-copilot-features-f2-requirements-pricing)
- [Prep data for AI: verified answers, AI instructions, AI data schemas (Microsoft Learn)](https://learn.microsoft.com/power-bi/create-reports/copilot-prepare-data-ai-verified-answers)
- [dbt agents (Developer Agent in preview, Analyst Agent in private beta)](https://docs.getdbt.com/docs/dbt-ai/dbt-agents) and [What's shipped in dbt, May 2026](https://www.getdbt.com/blog/what-s-shipped-in-dbt-may-2026)
- [pbixray: read `.pbix` metadata, DAX and Power Query in Python](https://pypi.org/project/pbixray)
- [Power BI external tools landscape (Tabular Editor BPA, Measure Killer, DAX Studio)](https://exceleratorbi.com.au/five-essential-power-tools-for-power-bi-desktop/)
- Claude model IDs and prices: Anthropic API model table, cached 25 Sep 2026
