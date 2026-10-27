# ADR 0001: Power BI first, local-first MVP

- Status: accepted
- Date: 2026-10-26

## Context

The original plan (docs/PLAN.md, 25 Oct) built a cross-tool platform: 9 modules, ~20 integrations and 6 surfaces, with text-to-SQL "Ask" first. The review (docs/REVIEW.md) found three problems with that. Text-to-SQL is the most crowded part of the market. dbt Labs is shipping its own agents. And Copilot in Power BI needs F2+/P1+ capacity, which leaves Pro-licence teams with no AI help at all.

## Decision

1. **Power BI first.** The MVP is a "Power BI model doctor": audit, fix, document, and make models AI-ready. Ask (over the semantic model, in DAX) and Narratives come next. SQL sources, dbt, Airflow, notebooks, Request Desk and Excel come later, and only on customer demand.
2. **Local-first.** The MVP runs on the user's Windows machine. PBIP/TMDL files stay on disk. Only model metadata (names, types, DAX expressions, descriptions) is sent to the model provider, never table data. A cloud/team version comes in Phase 3.
3. **No fixed team hours.** The team works on it as needed, so phases are gated by exit criteria, not dates.

## Consequences

- Phase 0 needs no Docker, Postgres, OIDC, Celery or sandbox. See ADR 0003.
- Every change targets files in a Git repository (the PBIP folder), so "apply" means a commit on a branch.
- Users whose PBIP folder isn't in Git need a guided "start tracking with Git" step (Phase 1).
- The local web UI must protect itself from other websites. It binds to 127.0.0.1 only, checks Host/Origin, and puts a CSRF token on every form.
- Team features (approval by a second person, shared context) wait for the cloud version.
