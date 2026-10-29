"""The `prep-for-ai` agent: propose Copilot synonyms, AI instructions and example prompts.

Everything lands in one change set of `copilot.*` edits (docs/specs/powerbi.md, "Copilot pack").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from datawright.agents.documenter import (
    KINDS,
    PATH_SCHEMA,
    DocContext,
    _get_object,
    _list_objects,
)
from datawright.agents.runtime import AgentRunner, MessagesClient
from datawright.agents.scripted import Message, last_tool_results, text, tool_calls
from datawright.agents.tools import (
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    customer_data,
    unwrap_customer_data,
)
from datawright.config import Settings
from datawright.connectors.powerbi import copilot
from datawright.connectors.powerbi.model import SemanticModel
from datawright.core.capabilities import Capability
from datawright.core.changesets import ChangeSetService
from datawright.models import AgentRun, ChangeSet, Connection, User
from datawright.services import connector_for

MODULE = "prep-for-ai"
SKILL_PATH = Path(__file__).parent / "skills" / "powerbi" / "prep-for-ai.md"
MAX_SYNONYMS_PER_OBJECT = 8


@dataclass
class PrepContext(DocContext):
    instructions_for: set[str] = field(default_factory=set)
    prompts_for: set[str] = field(default_factory=set)

    def ensure_change_set(self) -> ChangeSet:
        if self.change_set is None:
            self.change_set = self.service.create_draft(
                self.connection,
                f"Prep {self.connection.name} for Copilot (preview)",
                run_id=self.run.id if self.run else None,
            )
        return self.change_set


def copilot_overview(connector: Any, model: SemanticModel) -> dict[str, Any]:
    """What an agent needs to write AI instructions: current Copilot files plus the model's
    business vocabulary. Metadata only, never data rows."""
    current = connector.copilot_settings(model)
    visible = [t for t in model.tables.values() if not t.is_hidden and not t.is_auto_date]
    return {
        "model": model.name,
        "writable": connector.copilot_file(model, copilot.SCHEMA_FILE) is not None,
        "current": {
            "instructions": current.instructions,
            "example_prompts": current.example_prompts,
            "synonyms": current.synonyms,
            "files": current.files,
        },
        "tables": [
            {
                "table": t.name,
                "description": t.description,
                "data_category": t.data_category,
                "date_columns": [c.name for c in t.columns if (c.data_type or "") == "dateTime"],
            }
            for t in visible
        ][:60],
        "measures": [
            {
                "measure": m.name,
                "table": t.name,
                "description": m.description,
                "format_string": m.format_string,
                "display_folder": m.display_folder,
            }
            for t in visible
            for m in t.measures
            if not m.is_hidden
        ][:120],
    }


def _model(ctx: DocContext, name: str) -> SemanticModel:
    models = ctx.connector.models()
    if name not in models:
        raise ToolError(f"unknown model {name!r}; models: {', '.join(models)}")
    return models[name]


def _get_copilot_settings(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    models = [_model(ctx, args["model"])] if args.get("model") else ctx.connector.models().values()
    rows = [copilot_overview(ctx.connector, m) for m in models]
    return ToolResult(customer_data(rows), f"Copilot settings for {len(rows)} model(s)")


def _file(ctx: DocContext, model: SemanticModel, name: str) -> str:
    path = ctx.connector.copilot_file(model, name)
    if path is None:
        raise ToolError("this model is read-only (a .pbix); save it as PBIP to make changes")
    return path


def _propose_synonyms(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    if path in ctx.proposed:
        raise ToolError("synonyms for this object were already proposed in this run")
    if len(ctx.proposed) >= ctx.limit:
        raise ToolError(f"limit of {ctx.limit} objects reached; stop and summarise")
    try:
        ref = ctx.connector.resolve(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    if ref.is_hidden:
        raise ToolError("this object is hidden; Copilot doesn't use synonyms for hidden objects")
    try:
        synonyms = copilot.clean_synonyms(list(args.get("synonyms") or []), ref.name)
    except copilot.CopilotError as e:
        raise ToolError(str(e)) from e
    if not synonyms:
        raise ToolError("no usable synonyms (empty, or the same as the name)")
    if len(synonyms) > MAX_SYNONYMS_PER_OBJECT:
        raise ToolError(f"at most {MAX_SYNONYMS_PER_OBJECT} synonyms per object")
    intent = {
        "op": "copilot.set_synonyms",
        "kind": ref.kind,
        "table": ref.table.name,
        "name": ref.name,
        "synonyms": synonyms,
        "object_path": path,
    }
    ctx.service.add_edit(
        ctx.ensure_change_set(), _file(ctx, ref.model, copilot.SCHEMA_FILE), intent
    )
    ctx.proposed[path] = ", ".join(synonyms)
    return ToolResult(
        f"Proposed for review ({len(ctx.proposed)} of at most {ctx.limit}).",
        f"{path}: {', '.join(synonyms)}",
    )


def _propose_instructions(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    assert isinstance(ctx, PrepContext)
    model = _model(ctx, str(args.get("model", "")))
    if model.name in ctx.instructions_for:
        raise ToolError("AI instructions for this model were already proposed in this run")
    try:
        value = copilot.clean_instructions(str(args.get("instructions", "")))
    except copilot.CopilotError as e:
        raise ToolError(str(e)) from e
    intent = {
        "op": "copilot.set_instructions",
        "value": value,
        "object_path": f"{model.name}/copilot/instructions",
    }
    ctx.service.add_edit(
        ctx.ensure_change_set(), _file(ctx, model, copilot.INSTRUCTIONS_FILE), intent
    )
    ctx.instructions_for.add(model.name)
    return ToolResult("AI instructions proposed for review.", f"{len(value)} characters")


def _propose_prompts(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    assert isinstance(ctx, PrepContext)
    model = _model(ctx, str(args.get("model", "")))
    if model.name in ctx.prompts_for:
        raise ToolError("example prompts for this model were already proposed in this run")
    try:
        prompts = copilot.clean_prompts(list(args.get("prompts") or []))
    except copilot.CopilotError as e:
        raise ToolError(str(e)) from e
    intent = {
        "op": "copilot.set_example_prompts",
        "value": prompts,
        "object_path": f"{model.name}/copilot/examplePrompts",
    }
    ctx.service.add_edit(ctx.ensure_change_set(), _file(ctx, model, copilot.PROMPTS_FILE), intent)
    ctx.prompts_for.add(model.name)
    return ToolResult(f"{len(prompts)} example prompts proposed for review.", "; ".join(prompts))


MODEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["model"],
    "properties": {"model": {"type": "string"}},
}

REGISTRY = ToolRegistry(
    [
        Tool(
            "list_objects",
            "List tables, columns or measures in the connected model(s). Returns object paths for "
            "the other tools. Set missing_only to false to see every object.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "missing_only"],
                "properties": {
                    "kind": {"type": "string", "enum": ["all", *KINDS]},
                    "missing_only": {"type": "boolean"},
                },
            },
            Capability.READ,
            _list_objects,
        ),
        Tool(
            "get_object",
            "Read one table, column or measure: its DAX, type, related objects and relationships. "
            "Metadata only, never data rows.",
            PATH_SCHEMA,
            Capability.READ,
            _get_object,
        ),
        Tool(
            "get_copilot_settings",
            "Read a model's current Copilot settings (AI instructions, example prompts, synonyms) "
            "and its business vocabulary: visible tables, date columns and measures with their "
            "descriptions. Pass an empty model name for every model.",
            MODEL_SCHEMA,
            Capability.READ,
            _get_copilot_settings,
        ),
        Tool(
            "propose_synonyms",
            "Propose the synonyms Copilot should accept for one visible table, column or measure "
            "(replaces its current list). Goes into a change set for human review.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["object_path", "synonyms"],
                "properties": {
                    "object_path": {"type": "string"},
                    "synonyms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"1 to {MAX_SYNONYMS_PER_OBJECT} words people really use.",
                    },
                },
            },
            Capability.PROPOSE,
            _propose_synonyms,
        ),
        Tool(
            "propose_ai_instructions",
            "Propose the AI instructions for a model (replaces the current text). Markdown, at "
            f"most {copilot.MAX_INSTRUCTIONS} characters. Goes into a change set for review.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["model", "instructions"],
                "properties": {"model": {"type": "string"}, "instructions": {"type": "string"}},
            },
            Capability.PROPOSE,
            _propose_instructions,
        ),
        Tool(
            "propose_example_prompts",
            "Propose example prompts Copilot shows users for a model (replaces the current list). "
            f"Each at most {copilot.MAX_PROMPT} characters. Goes into a change set for review.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["model", "prompts"],
                "properties": {
                    "model": {"type": "string"},
                    "prompts": {"type": "array", "items": {"type": "string"}},
                },
            },
            Capability.PROPOSE,
            _propose_prompts,
        ),
    ]
)


def run_prep(
    session: Session,
    settings: Settings,
    client: MessagesClient,
    conn: Connection,
    user: User,
    *,
    limit: int = 15,
    model_label: str | None = None,
) -> AgentRun:
    connector = connector_for(conn)
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    permission = min(Capability.parse(conn.capability), Capability.PROPOSE)
    prompt = (
        f"Prepare the model(s) in connection '{conn.name}' for Copilot: propose AI instructions, "
        f"up to 5 example prompts, and synonyms for at most {limit} visible objects people are "
        "likely to call by another name. Then summarise."
    )
    run = AgentRun(
        workspace_id=user.workspace_id,
        user_id=user.id,
        connection_id=conn.id,
        module=MODULE,
        prompt=prompt,
        permission=permission.label,
        model=model_label or settings.model,
    )
    session.add(run)
    session.flush()
    service = ChangeSetService(session, user)
    runner = AgentRunner(session, client, settings)
    ctx = PrepContext(session, conn, connector, service, run, limit, KINDS)
    runner.run(
        run,
        system=SKILL_PATH.read_text(encoding="utf-8"),
        user_prompt=prompt,
        registry=REGISTRY,
        ctx=ctx,
    )
    cs = ctx.change_set
    if cs is not None and run.status == "succeeded":
        problems = service.propose(cs)
        runner.check(
            run, "change set validated" if not problems else "; ".join(problems), ok=not problems
        )
    elif cs is not None:
        runner.check(run, f"run {run.status}; change set left as draft", ok=False)
    session.commit()
    return run


# ---------------------------------------------------------------- offline mode


def draft_synonyms(kind: str, name: str) -> list[str]:
    """Rule-based placeholders used only offline: other forms of the name people type."""
    words = name.strip()
    out: list[str] = []
    if kind == "measure":
        m = re.fullmatch(r"(?i)(total|sum of)\s+(.+)", words)
        if m:
            out.append(m.group(2).lower())
        m = re.fullmatch(r"(?i)(.+?)\s+count", words)
        if m:
            out += [f"number of {m.group(1).lower()}s", f"how many {m.group(1).lower()}s"]
        m = re.fullmatch(r"(?i)(avg|average)\s+(.+)", words)
        if m:
            out.append(("average " if m.group(1).lower() == "avg" else "avg ") + m.group(2).lower())
        if words.endswith("%"):
            out.append(words[:-1].strip().lower() + " percent")
    elif kind == "table" and words.isalpha():
        out.append(words.lower() + "s" if not words.lower().endswith("s") else words.lower()[:-1])
    return [s for s in out if s and s.casefold() != name.casefold()]


def draft_instructions(overview: dict[str, Any]) -> str:
    lines = [
        "<!-- Offline draft written by rules, not Claude. Review every line before approving. -->",
        "",
        f"# {overview['model']}: notes for Copilot",
        "",
    ]
    dates = [f"{t['table']}[{c}]" for t in overview["tables"] for c in t["date_columns"]]
    date_tables = [t["table"] for t in overview["tables"] if t.get("data_category") == "Time"]
    if date_tables:
        lines.append(f"- Use the {date_tables[0]} table for dates, months and years.")
    elif dates:
        lines.append(f"- Date columns: {', '.join(dates[:6])}.")
    described = [m for m in overview["measures"] if m.get("description")]
    if described:
        lines += ["", "## Measures", ""]
        lines += [f"- [{m['measure']}]: {m['description']}" for m in described[:40]]
    return "\n".join(lines) + "\n"


def draft_prompts(overview: dict[str, Any]) -> list[str]:
    measures = [m["measure"] for m in overview["measures"]][:3]
    return [f"What is {m} this year compared with last year?" for m in measures] or [
        "What does this model contain?"
    ]


def offline_script(limit: int) -> Any:
    def script(messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> Message:
        tools = {t["name"] for t in kwargs.get("tools", [])}
        turn = sum(1 for m in messages if m["role"] == "assistant")
        results = last_tool_results(messages)
        if turn == 0:
            return tool_calls(
                ("get_copilot_settings", {"model": ""}),
                ("list_objects", {"kind": "all", "missing_only": False}),
            )
        if turn == 1:
            if "propose_synonyms" not in tools:
                return text("This connection is read-only, so I can't propose Copilot settings.")
            overviews = json.loads(unwrap_customer_data(results[0]["content"]))
            rows = json.loads(unwrap_customer_data(results[1]["content"]))
            calls: list[tuple[str, dict[str, Any]]] = []
            for o in overviews:
                if not o["writable"]:
                    continue
                calls.append(
                    (
                        "propose_ai_instructions",
                        {"model": o["model"], "instructions": draft_instructions(o)},
                    )
                )
                calls.append(
                    ("propose_example_prompts", {"model": o["model"], "prompts": draft_prompts(o)})
                )
            for r in rows:
                synonyms = draft_synonyms(r["kind"], r["name"])
                if synonyms and r["kind"] != "column" and not r.get("hidden"):
                    calls.append(
                        (
                            "propose_synonyms",
                            {"object_path": r["object_path"], "synonyms": synonyms},
                        )
                    )
                if sum(1 for c in calls if c[0] == "propose_synonyms") >= limit:
                    break
            return tool_calls(*calls) if calls else text("Nothing to propose.")
        n = sum(1 for r in results if not r.get("is_error"))
        return text(f"Proposed {n} offline Copilot drafts for review (preview).")

    return script
