"""The `ask-model` agent: answer questions about a model from its metadata, citing objects."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from datawright.agents.documenter import describe_object
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
from datawright.connectors.powerbi.pbip import PowerBIConnector
from datawright.connectors.powerbi.search import search
from datawright.core.capabilities import Capability
from datawright.models import AgentRun, Connection, User
from datawright.services import connector_for

MODULE = "ask-model"
SKILL_PATH = Path(__file__).parent / "skills" / "powerbi" / "ask-model.md"


@dataclass
class AskContext:
    connector: PowerBIConnector


def _search(ctx: AskContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("query is empty")
    hits = search(ctx.connector, query, limit=12)
    return ToolResult(customer_data(hits), f"{len(hits)} hits for {query!r}")


def _get(ctx: AskContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    try:
        ref = ctx.connector.resolve(path)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e
    return ToolResult(customer_data(describe_object(ctx.connector, ref)), f"read {path}")


def _relationships(ctx: AskContext, args: dict[str, Any]) -> ToolResult:
    table = str(args.get("table", "")).strip()
    rows = [
        {
            "from": f"{r.from_table}[{r.from_column}]",
            "to": f"{r.to_table}[{r.to_column}]",
            "cardinality": f"{r.from_cardinality} to {r.to_cardinality}",
            "active": r.is_active,
            "direction": r.cross_filtering,
        }
        for m in ctx.connector.models().values()
        for r in m.relationships
        if not table or table in (r.from_table, r.to_table)
    ]
    return ToolResult(customer_data(rows), f"{len(rows)} relationships")


REGISTRY = ToolRegistry(
    [
        Tool(
            "search_model",
            "Search table, column and measure names, descriptions and DAX by "
            "keywords. Returns object paths.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
            Capability.READ,
            _search,
        ),
        Tool(
            "get_object",
            "Read one table, column or measure: DAX, type, description, relationships "
            "and what uses it. Metadata only.",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["object_path"],
                "properties": {"object_path": {"type": "string"}},
            },
            Capability.READ,
            _get,
        ),
        Tool(
            "list_relationships",
            "List relationships, optionally only those touching one table "
            "(pass an empty string for all).",
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["table"],
                "properties": {"table": {"type": "string"}},
            },
            Capability.READ,
            _relationships,
        ),
    ]
)


def run_ask(
    session: Session,
    settings: Settings,
    client: MessagesClient,
    conn: Connection,
    user: User,
    question: str,
    *,
    model_label: str | None = None,
) -> AgentRun:
    question = question.strip()
    if not question:
        raise ValueError("ask a question")
    connector = connector_for(conn)
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    run = AgentRun(
        workspace_id=user.workspace_id,
        user_id=user.id,
        connection_id=conn.id,
        module=MODULE,
        prompt=question,
        permission=Capability.READ.label,
        model=model_label or settings.model,
    )
    session.add(run)
    session.flush()
    prompt = f"Question about the model in connection '{conn.name}':\n\n{question}"
    AgentRunner(session, client, settings).run(
        run,
        system=SKILL_PATH.read_text(encoding="utf-8"),
        user_prompt=prompt,
        registry=REGISTRY,
        ctx=AskContext(connector),
    )
    session.commit()
    return run


def offline_script(messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> Message:
    """Offline stand-in: search with the question, read the top hit, report what was found."""
    turn = sum(1 for m in messages if m["role"] == "assistant")
    results = last_tool_results(messages)
    if turn == 0:
        question = str(messages[0]["content"]).split("\n\n", 1)[-1]
        return tool_calls(("search_model", {"query": question}))
    if turn == 1:
        hits = json.loads(unwrap_customer_data(results[0]["content"]))
        if not hits:
            return text("Offline answer: nothing in the model matches those words.")
        return tool_calls(*[("get_object", {"object_path": h["object_path"]}) for h in hits[:2]])
    found = [
        json.loads(unwrap_customer_data(r["content"])) for r in results if not r.get("is_error")
    ]
    lines = [f"- {f['object_path']}: {f.get('description') or 'no description'}" for f in found]
    return text(
        "Offline answer (no model was used): the closest matches are\n"
        + "\n".join(lines)
        + "\n\nSources: "
        + ", ".join(f["object_path"] for f in found)
    )
