# ADR 0002: Agent runtime and model

- Status: accepted
- Date: 2026-10-26

## Context

The plan named the Claude Agent SDK as the runtime, and three models: `claude-sonnet-5-5`, `claude-haiku-5-5` (which doesn't exist) and `claude-opus-5-5`. Datawright's agents only use our own tools: read the model, propose a change. Every step must be recorded (AgentStep), and every tool call must be checked against the run's permission level. Tests must run without an API key.

## Decision

- **Runtime:** the Messages API from the `anthropic` Python SDK, with our own short tool-call loop in `datawright.agents.runtime`. We chose our own loop over the beta tool runner because it keeps the beta out of the core path, makes recording and permission checks one obvious place, and lets tests inject a scripted client.
- **Model:** one model to start, `claude-sonnet-5-5`, which the `DATAWRIGHT_MODEL` setting can change. No router until cost per accepted task shows a measured saving (prompt caches are per model, so routing loses cache reuse). The small model, if one is ever needed, is `claude-haiku-4-5`.
- **Tools** are defined with `strict: true` schemas. `tool_choice` stays `auto`, because forced tool choice is rejected on current models.
- **Refusal fallback:** server-side `fallbacks: "default"` is on by default when the installed SDK supports it, and can be switched off with `DATAWRIGHT_FALLBACKS=false`.
- **Tool output is data.** Text read from customer files (descriptions, DAX, M code) is wrapped in `<customer_data>` tags. The system prompt says it is data, never instructions.
- **Later options:** the Claude Agent SDK for the Windows desktop agent (Phase 2), and Managed Agents for hosted runs in the cloud version (Phase 3+). Each gets its own ADR when adopted.

## Consequences

- The loop must handle `end_turn`, `tool_use`, `max_tokens`, `refusal` and `pause_turn`. It appends the full `response.content` (append-only history), so thinking blocks survive.
- Cost and tokens are recorded per run from `response.usage`.
