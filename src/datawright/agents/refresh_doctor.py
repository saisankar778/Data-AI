"""Refresh doctor: deterministic diagnosis, plus an optional Claude explanation."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from datawright.agents.ask import REGISTRY as ASK_REGISTRY
from datawright.agents.ask import AskContext
from datawright.agents.runtime import AgentRunner, MessagesClient
from datawright.agents.tools import Tool, ToolError, ToolRegistry, ToolResult, customer_data
from datawright.config import Settings
from datawright.connectors.powerbi.pbip import PowerBIConnector
from datawright.connectors.powerbi.refresh import Diagnosis, diagnose, m_steps
from datawright.core.capabilities import Capability
from datawright.core.paths import split_path
from datawright.models import AgentRun, Connection, User
from datawright.services import connector_for

MODULE = "refresh-doctor"
SKILL_PATH = Path(__file__).parent / "skills" / "powerbi" / "refresh-doctor.md"
MAX_ERROR_CHARS = 8000


def query_code(connector: PowerBIConnector, object_path: str) -> dict[str, Any]:
    """M code of a partition (`Model/Table/partition/Name`) or shared query
    (`Model/expression/Name`), split into steps."""
    parts = split_path(object_path)
    model = connector.models().get(parts[0]) if parts else None
    code = None
    if model and len(parts) == 4 and parts[2] == "partition":
        table = model.tables.get(parts[1])
        part = next((p for p in table.partitions if p.name == parts[3]), None) if table else None
        code = part.source if part else None
    elif model and len(parts) == 3 and parts[1] == "expression":
        code = model.expressions.get(parts[2])
    if code is None:
        raise KeyError(f"no Power Query code at {object_path!r}")
    return {"object_path": object_path, "code": code,
            "steps": [{"step": s, "code": c} for s, c in m_steps(code)]}  # fmt: skip


def _get_query(ctx: AskContext, args: dict[str, Any]) -> ToolResult:
    path = str(args.get("object_path", ""))
    try:
        return ToolResult(customer_data(query_code(ctx.connector, path)), f"read {path}")
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e


REGISTRY = ToolRegistry([
    *ASK_REGISTRY.tools,
    Tool("get_query", "Read the Power Query (M) code of a table partition "
         "(Model/Table/partition/Name) or a shared query (Model/expression/Name), split into "
         "steps.",
         {"type": "object", "additionalProperties": False, "required": ["object_path"],
          "properties": {"object_path": {"type": "string"}}},
         Capability.READ, _get_query),
])  # fmt: skip


def diagnose_connection(conn: Connection, error_text: str) -> list[Diagnosis]:
    return diagnose(error_text[:MAX_ERROR_CHARS], list(connector_for(conn).models().values()))


def explain(
    session: Session, settings: Settings, client: MessagesClient, conn: Connection, user: User,
    error_text: str, diagnoses: list[Diagnosis], *, model_label: str | None = None,
) -> AgentRun:  # fmt: skip
    """Ask Claude for a tailored explanation, grounded in the deterministic diagnosis."""
    connector = connector_for(conn)
    first = [asdict(d) for d in diagnoses] or "no known pattern matched"
    prompt = (
        f"Refresh error from connection '{conn.name}':\n\n<customer_data>\n"
        f"{error_text[:MAX_ERROR_CHARS]}\n</customer_data>\n\n"
        f"First diagnosis from Datawright's pattern library:\n<customer_data>\n"
        f"{json.dumps(first, ensure_ascii=False, indent=1)}\n</customer_data>"
    )
    run = AgentRun(workspace_id=user.workspace_id, user_id=user.id, connection_id=conn.id,
                   module=MODULE, prompt=prompt[:4000], permission=Capability.READ.label,
                   model=model_label or settings.model)  # fmt: skip
    session.add(run)
    session.flush()
    AgentRunner(session, client, settings).run(
        run, system=SKILL_PATH.read_text(encoding="utf-8"), user_prompt=prompt,
        registry=REGISTRY, ctx=AskContext(connector),
    )  # fmt: skip
    session.commit()
    return run
