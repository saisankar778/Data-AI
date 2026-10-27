"""The `document-measures` agent: propose descriptions for undocumented measures."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from datawright.connectors.powerbi.pbip import PbipRepoConnector
from datawright.core.capabilities import Capability
from datawright.core.changesets import ChangeSetService
from datawright.models import AgentRun, ChangeSet, Connection, Finding, User
from datawright.services import connector_for

MODULE = "document-measures"
SKILL_PATH = Path(__file__).parent / "skills" / "powerbi" / "document-measures.md"
MAX_DESCRIPTION = 300
MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")
COLUMN_REF = re.compile(r"('(?:[^']|'')+'|[A-Za-z_][\w ]*?)\[([^\]]+)\]")


@dataclass
class DocContext:
    session: Session
    connection: Connection
    connector: PbipRepoConnector
    service: ChangeSetService
    run: AgentRun
    limit: int
    change_set: ChangeSet | None = None
    proposed: dict[str, str] = field(default_factory=dict)

    def ensure_change_set(self) -> ChangeSet:
        if self.change_set is None:
            self.change_set = self.service.create_draft(
                self.connection, f"Describe measures in {self.connection.name}", run_id=self.run.id
            )
        return self.change_set


def _list_measures(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    missing_only = bool(args.get("missing_only", True))
    rows = [
        {
            "object_path": r.object_path,
            "table": r.table.name,
            "measure": r.measure.name,
            "has_description": bool(r.measure.description),
        }
        for r in ctx.connector.measures()
        if not missing_only or not r.measure.description
    ]
    return ToolResult(customer_data(rows), f"{len(rows)} measures listed")


def _get_measure(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    try:
        ref = ctx.connector.measure(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    others = {m.name: m for m in ref.table.measures}
    for t in ref.model.tables.values():
        for m in t.measures:
            others.setdefault(m.name, m)
    referenced = []
    for name in dict.fromkeys(MEASURE_REF.findall(ref.measure.expression)):
        other = others.get(name)
        if other is not None and other.name != ref.measure.name:
            referenced.append(
                {
                    "measure": other.name,
                    "expression": other.expression[:400],
                    "description": other.description,
                }
            )
    payload = {
        "object_path": path,
        "table": ref.table.name,
        "measure": ref.measure.name,
        "expression": ref.measure.expression,
        "format_string": ref.measure.format_string,
        "display_folder": ref.measure.display_folder,
        "table_columns": [
            {"column": c.name, "data_type": c.data_type, "description": c.description}
            for c in ref.table.columns
        ],
        "referenced_measures": referenced,
    }
    return ToolResult(customer_data(payload), f"read {path}")


def _propose_description(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    desc = " ".join(str(args.get("description", "")).split())
    if not desc:
        raise ToolError("description is empty")
    if len(desc) > MAX_DESCRIPTION:
        raise ToolError(f"description is {len(desc)} characters; the limit is {MAX_DESCRIPTION}")
    if "```" in desc:
        raise ToolError("description may not contain ```")
    if path in ctx.proposed:
        raise ToolError("a description for this measure was already proposed in this run")
    if len(ctx.proposed) >= ctx.limit:
        raise ToolError(f"limit of {ctx.limit} descriptions reached; stop and summarise")
    try:
        ref = ctx.connector.measure(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    intent = {
        "op": "tmdl.set_description",
        "kind": "measure",
        "table": ref.table.name,
        "name": ref.measure.name,
        "value": desc,
        "object_path": path,
    }
    ctx.service.add_edit(ctx.ensure_change_set(), ref.file_path, intent)
    ctx.proposed[path] = desc
    return ToolResult(
        f"Proposed for review ({len(ctx.proposed)} of at most {ctx.limit}).", f"{path}: {desc}"
    )


REGISTRY = ToolRegistry(
    [
        Tool(
            "list_measures",
            "List measures in the connected semantic model(s). Returns object paths to use "
            "with the other tools.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["missing_only"],
                "properties": {
                    "missing_only": {
                        "type": "boolean",
                        "description": "Only measures without a description.",
                    }
                },
            },
            Capability.READ,
            _list_measures,
        ),
        Tool(
            "get_measure",
            "Read one measure: DAX expression, format string, display folder, its table's columns "
            "and the measures it references. Metadata only, no data.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["object_path"],
                "properties": {"object_path": {"type": "string"}},
            },
            Capability.READ,
            _get_measure,
        ),
        Tool(
            "propose_description",
            "Propose a description for one measure. It is added to a change set for human review; "
            "nothing is written until a person approves it.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["object_path", "description"],
                "properties": {
                    "object_path": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": f"Plain sentences, at most {MAX_DESCRIPTION} chars.",
                    },
                },
            },
            Capability.PROPOSE,
            _propose_description,
        ),
    ]
)


def run_documenter(
    session: Session,
    settings: Settings,
    client: MessagesClient,
    conn: Connection,
    user: User,
    *,
    limit: int = 5,
    model_label: str | None = None,
) -> AgentRun:
    connector = connector_for(conn)
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    permission = min(Capability.parse(conn.capability), Capability.PROPOSE)
    prompt = (
        f"Document the measures that have no description in connection '{conn.name}'. "
        f"Propose at most {limit} descriptions, then summarise."
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
    ctx = DocContext(session, conn, connector, service, run, limit)
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
        if not problems:
            _link_findings(session, conn, cs)
    elif cs is not None:
        runner.check(run, f"run {run.status}; change set left as draft", ok=False)
    session.commit()
    return run


def _link_findings(session: Session, conn: Connection, cs: ChangeSet) -> None:
    for item in cs.items:
        for intent in item.intents:
            path = intent.get("object_path")
            finding = session.scalar(
                select(Finding).where(
                    Finding.connection_id == conn.id,
                    Finding.rule == "measure-missing-description",
                    Finding.object_path == path,
                    Finding.status == "open",
                    Finding.change_item_id.is_(None),
                )
            )
            if finding is not None:
                finding.change_item_id = item.id


# ---------------------------------------------------------------- offline mode

_FUNCS = {
    "SUM": "Total of {}",
    "AVERAGE": "Average of {}",
    "COUNTROWS": "Number of rows in {}",
    "DISTINCTCOUNT": "Number of distinct {} values",
    "MIN": "Smallest {}",
    "MAX": "Largest {}",
    "COUNT": "Count of {}",
}


def draft_description(payload: dict[str, Any]) -> str:
    """Rule-based placeholder used only offline; clearly labelled so nobody mistakes it."""
    expr = " ".join(str(payload.get("expression", "")).split())
    m = re.fullmatch(r"(\w+)\((.*)\)", expr)
    if m and m.group(1).upper() in _FUNCS and "(" not in m.group(2):
        base = _FUNCS[m.group(1).upper()].format(m.group(2).strip())
    elif (
        m
        and m.group(1).upper() == "DIVIDE"
        and m.group(2).count(",") == 1
        and "(" not in m.group(2)
    ):
        a, b = (s.strip() for s in m.group(2).split(","))
        base = f"Ratio of {a} to {b}"
    else:
        refs = [f"[{r}]" for r in dict.fromkeys(MEASURE_REF.findall(expr))]
        refs += [f"{t}[{c}]" for t, c in dict.fromkeys(COLUMN_REF.findall(expr))]
        base = (
            ("Calculated from " + ", ".join(refs[:4]))
            if refs
            else f"Calculated measure in the {payload.get('table')} table"
        )
    return f"Offline draft: {base}."[:MAX_DESCRIPTION]


def offline_script(limit: int) -> Any:
    def script(messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> Message:
        tools = {t["name"] for t in kwargs.get("tools", [])}
        turn = sum(1 for m in messages if m["role"] == "assistant")
        results = last_tool_results(messages)
        if turn == 0:
            return tool_calls(("list_measures", {"missing_only": True}))
        if turn == 1:
            rows = json.loads(unwrap_customer_data(results[0]["content"]))
            if not rows:
                return text("Every measure already has a description.")
            return tool_calls(
                *[("get_measure", {"object_path": r["object_path"]}) for r in rows[:limit]]
            )
        if turn == 2:
            if "propose_description" not in tools:
                return text("This connection is read-only, so I can't propose descriptions.")
            calls = []
            for r in results:
                if r.get("is_error"):
                    continue
                payload = json.loads(unwrap_customer_data(r["content"]))
                calls.append(
                    (
                        "propose_description",
                        {
                            "object_path": payload["object_path"],
                            "description": draft_description(payload),
                        },
                    )
                )
            return tool_calls(*calls)
        n = sum(1 for r in results if not r.get("is_error"))
        return text(f"Proposed {n} offline draft descriptions for review.")

    return script
