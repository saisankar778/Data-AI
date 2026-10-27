"""The `document-model` agent: propose descriptions for undocumented tables, columns, measures."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
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
from datawright.connectors.powerbi.dax import references
from datawright.connectors.powerbi.pbip import ObjectRef, PowerBIConnector
from datawright.core.capabilities import Capability
from datawright.core.changesets import ChangeSetService
from datawright.models import AgentRun, ChangeSet, Connection, Finding, User
from datawright.services import connector_for

MODULE = "document-model"
SKILL_PATH = Path(__file__).parent / "skills" / "powerbi" / "document-model.md"
MAX_DESCRIPTION = 300
KINDS = ("measure", "column", "table")
DESCRIPTION_RULES = ("measure-missing-description", "object-missing-description")
_M_FUNCTION = re.compile(r"\b([A-Z][A-Za-z]+\.[A-Z][A-Za-z]+)\s*\(")


@dataclass
class DocContext:
    session: Session
    connection: Connection
    connector: PowerBIConnector
    service: ChangeSetService
    run: AgentRun
    limit: int
    kinds: tuple[str, ...]
    change_set: ChangeSet | None = None
    proposed: dict[str, str] = field(default_factory=dict)

    def ensure_change_set(self) -> ChangeSet:
        if self.change_set is None:
            self.change_set = self.service.create_draft(
                self.connection, f"Describe objects in {self.connection.name}", run_id=self.run.id
            )
        return self.change_set


def undocumented(connector: PowerBIConnector, kinds: Sequence[str]) -> list[ObjectRef]:
    """Visible objects without a description, measures first (they matter most to readers)."""
    order = {k: i for i, k in enumerate(KINDS)}
    refs = [
        o
        for o in connector.objects()
        if o.kind in kinds
        and not o.is_hidden
        and not o.table.is_auto_date
        and not (o.description or "").strip()
    ]
    return sorted(refs, key=lambda o: order[o.kind])


def describe_object(connector: PowerBIConnector, o: ObjectRef) -> dict[str, Any]:
    """Metadata an agent (or an MCP client) needs to describe one object. Never table data."""
    model, t = o.model, o.table
    rels = [
        {
            "from": f"{r.from_table}[{r.from_column}]",
            "to": f"{r.to_table}[{r.to_column}]",
            "active": r.is_active,
            "direction": r.cross_filtering,
        }
        for r in model.relationships
        if t.name in (r.from_table, r.to_table)
    ]
    base: dict[str, Any] = {
        "object_path": o.object_path,
        "kind": o.kind,
        "table": t.name,
        "description": o.description,
    }
    if o.measure is not None:
        m = o.measure
        measures = {x.name: x for _, x in model.all_measures()}
        refs = references(m.expression)
        return base | {
            "measure": m.name,
            "expression": m.expression,
            "format_string": m.format_string,
            "display_folder": m.display_folder,
            "referenced_measures": [
                {
                    "measure": n,
                    "expression": measures[n].expression[:400],
                    "description": measures[n].description,
                }
                for n in dict.fromkeys(
                    r.name for r in refs if r.name in measures and r.table is None
                )
                if n != m.name
            ],
            "referenced_columns": sorted({f"{r.table}[{r.name}]" for r in refs if r.table}),
            "table_columns": [
                {"column": c.name, "data_type": c.data_type, "description": c.description}
                for c in t.columns
            ][:60],
        }
    if o.column is not None:
        c = o.column
        used_by = [
            {"measure": x.name, "expression": x.expression[:300]}
            for _, x in model.all_measures()
            if any(r.name == c.name and r.table in (t.name, None) for r in references(x.expression))
        ][:10]
        return base | {
            "column": c.name,
            "data_type": c.data_type,
            "column_kind": c.kind,
            "expression": c.expression,
            "format_string": c.format_string,
            "data_category": c.data_category,
            "sort_by_column": c.sort_by_column,
            "is_key": c.is_key,
            "table_description": t.description,
            "other_columns": [x.name for x in t.columns if x.name != c.name][:60],
            "relationships": [r for r in rels if f"{t.name}[{c.name}]" in (r["from"], r["to"])],
            "used_by_measures": used_by,
        }
    sources = sorted({f for p in t.partitions for f in _M_FUNCTION.findall(p.source or "")})
    return base | {
        "columns": [
            {
                "column": c.name,
                "data_type": c.data_type,
                "hidden": c.is_hidden,
                "description": c.description,
            }
            for c in t.columns
        ][:80],
        "measures": [x.name for x in t.measures][:80],
        "relationships": rels,
        "load": {
            "partition_kinds": sorted({p.kind or "?" for p in t.partitions}),
            "power_query_functions": sources,
        },
        "data_category": t.data_category,
    }


def _list_objects(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    kind = str(args.get("kind", "all"))
    kinds = ctx.kinds if kind == "all" else (kind,)
    if any(k not in KINDS for k in kinds):
        raise ToolError(f"kind must be one of: all, {', '.join(KINDS)}")
    if args.get("missing_only", True):
        refs = undocumented(ctx.connector, kinds)
    else:
        refs = [o for o in ctx.connector.objects() if o.kind in kinds]
    rows = [
        {
            "object_path": o.object_path,
            "kind": o.kind,
            "table": o.table.name,
            "name": o.name,
            "has_description": bool(o.description),
        }
        for o in refs[:200]
    ]
    return ToolResult(customer_data(rows), f"{len(rows)} objects listed")


def _get_object(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    try:
        ref = ctx.connector.resolve(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    return ToolResult(customer_data(describe_object(ctx.connector, ref)), f"read {path}")


def check_description(desc: str) -> str:
    desc = " ".join(desc.split())
    if not desc:
        raise ToolError("description is empty")
    if len(desc) > MAX_DESCRIPTION:
        raise ToolError(f"description is {len(desc)} characters; the limit is {MAX_DESCRIPTION}")
    if "```" in desc:
        raise ToolError("description may not contain ```")
    return desc


def description_intent(ref: ObjectRef, desc: str) -> dict[str, Any]:
    if ref.file_path is None:
        raise ToolError("this model is read-only (a .pbix); save it as PBIP to make changes")
    return {
        "op": "tmdl.set_description",
        "kind": ref.kind,
        "table": None if ref.kind == "table" else ref.table.name,
        "name": ref.name,
        "value": desc,
        "object_path": ref.object_path,
    }


def _propose_description(ctx: DocContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    desc = check_description(str(args.get("description", "")))
    if path in ctx.proposed:
        raise ToolError("a description for this object was already proposed in this run")
    if len(ctx.proposed) >= ctx.limit:
        raise ToolError(f"limit of {ctx.limit} descriptions reached; stop and summarise")
    try:
        ref = ctx.connector.resolve(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    if ref.kind not in ctx.kinds:
        raise ToolError(f"this run documents {', '.join(ctx.kinds)} only")
    ctx.service.add_edit(
        ctx.ensure_change_set(), ref.file_path or "", description_intent(ref, desc)
    )
    ctx.proposed[path] = desc
    return ToolResult(
        f"Proposed for review ({len(ctx.proposed)} of at most {ctx.limit}).", f"{path}: {desc}"
    )


PATH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object_path"],
    "properties": {"object_path": {"type": "string"}},
}

REGISTRY = ToolRegistry(
    [
        Tool(
            "list_objects",
            "List tables, columns or measures in the connected model(s). Returns object paths for "
            "the other tools. Use missing_only to see only visible objects without a description.",
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
            "propose_description",
            "Propose a description for one table, column or measure. It goes into a change set for "
            "human review; nothing is written until a person approves it.",
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
    limit: int = 10,
    kinds: Sequence[str] = KINDS,
    model_label: str | None = None,
) -> AgentRun:
    kinds = tuple(k for k in KINDS if k in kinds)
    if not kinds:
        raise ValueError(f"kinds must include at least one of {', '.join(KINDS)}")
    connector = connector_for(conn)
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    permission = min(Capability.parse(conn.capability), Capability.PROPOSE)
    plural = ", ".join(f"{k}s" for k in kinds)
    prompt = (
        f"Document the {plural} that have no description in connection '{conn.name}'. "
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
    ctx = DocContext(session, conn, connector, service, run, limit, kinds)
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
            link_findings(session, conn, cs)
    elif cs is not None:
        runner.check(run, f"run {run.status}; change set left as draft", ok=False)
    session.commit()
    return run


def link_findings(session: Session, conn: Connection, cs: ChangeSet) -> None:
    """Tie each proposed description to the open finding it fixes."""
    for item in cs.items:
        for intent in item.intents:
            finding = session.scalar(
                select(Finding).where(
                    Finding.connection_id == conn.id,
                    Finding.rule.in_(DESCRIPTION_RULES),
                    Finding.object_path == intent.get("object_path"),
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
    kind = payload.get("kind")
    if kind == "table":
        base = (
            f"Table with {len(payload.get('columns', []))} columns and "
            f"{len(payload.get('measures', []))} measures"
        )
    elif kind == "column":
        base = (
            f"{payload.get('data_type') or 'Unknown type'} column {payload.get('column')} "
            f"in {payload.get('table')}"
        )
    else:
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
            refs = [
                f"[{r.name}]" if r.table is None else f"{r.table}[{r.name}]"
                for r in dict.fromkeys(references(expr))
            ]
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
            return tool_calls(("list_objects", {"kind": "all", "missing_only": True}))
        if turn == 1:
            rows = json.loads(unwrap_customer_data(results[0]["content"]))
            if not rows:
                return text("Every visible object already has a description.")
            return tool_calls(
                *[("get_object", {"object_path": r["object_path"]}) for r in rows[:limit]]
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
