from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from datawright.config import Settings
from datawright.mcp_server import DatawrightTools, build_server
from datawright.models import AgentRun, AgentStep, ChangeSet, Connection, Finding


@pytest.fixture
def tools(settings: Settings, sessions: sessionmaker[Session], conn: Connection) -> DatawrightTools:
    return DatawrightTools(settings, sessions)


def test_read_tools(tools: DatawrightTools) -> None:
    assert tools.list_connections() == [
        {"name": "test", "kind": "pbip_repo", "path": tools.list_connections()[0]["path"],
         "allows": "apply"}
    ]  # fmt: skip
    health = tools.model_health("test")[0]
    assert health["model"] == "Retail" and health["score"] == 0
    assert health["findings_by_rule"]["measure-missing-description"] == 11
    divide = tools.list_findings("test", rule="use-divide-function")
    assert {f["object_path"] for f in divide} == {
        "Retail/Sales/measure/Margin %", "Retail/Sales/measure/Sales YoY %"
    }  # fmt: skip
    hits = tools.search_model("test", "margin")
    assert hits[0]["object_path"] == "Retail/Sales/measure/Margin %"
    obj = tools.get_object("test", "Retail/Sales/measure/Margin %")
    assert obj["expression"] == "[Profit] / [Total Sales]"
    assert len(tools.list_undocumented("test", kind="measure")) == 11


def test_propose_and_submit(tools: DatawrightTools, session: Session) -> None:
    msg = tools.propose_description("test", "Retail/Sales/measure/Total Sales", "Total sales.")
    assert "draft change set" in msg
    tools.propose_description("test", "Retail/Sales/column/UnitPrice", "Price per unit.")
    with pytest.raises(ValueError, match="already has a proposal"):
        tools.propose_description("test", "Retail/Sales/measure/Total Sales", "Again.")
    with pytest.raises(ValueError, match="object not found"):
        tools.propose_description("test", "Retail/Nope", "x")
    with pytest.raises(ValueError, match="limit is 300"):
        tools.propose_description("test", "Retail/Sales/measure/Profit", "x" * 301)
    result = tools.submit_for_review("test")
    assert result["status"] == "proposed" and result["edits"] == 2
    assert result["review_url"].endswith(f"/change-sets/{result['change_set']}")
    cs = session.get(ChangeSet, result["change_set"])
    assert cs is not None and cs.status == "proposed"
    linked = session.scalars(select(Finding).where(Finding.change_item_id.is_not(None))).all()
    assert len(linked) == 2
    with pytest.raises(ValueError, match="no draft"):
        tools.submit_for_review("test")


def test_every_call_is_audited(tools: DatawrightTools, session: Session) -> None:
    tools.list_connections()
    with pytest.raises(ValueError):
        tools.get_object("test", "Retail/Nope")
    run = session.scalar(select(AgentRun).where(AgentRun.module == "mcp-session"))
    assert run is not None
    steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run.id)).all()
    assert [s.tool for s in steps] == ["list_connections", "get_object"]
    assert [s.is_error for s in steps] == [False, True]


def test_read_only_connection_refuses_proposals(
    tools: DatawrightTools, session: Session, conn: Connection
) -> None:
    conn.capability = "read"
    session.commit()
    with pytest.raises(ValueError, match="read-only"):
        tools.propose_description("test", "Retail/Sales/measure/Total Sales", "x")


def test_server_registers_no_write_tools(settings: Settings, conn: Connection) -> None:
    server = build_server(settings)

    async def go() -> tuple[set[str], str]:
        names = {t.name for t in await server.list_tools()}
        result = await server.call_tool("model_health", {"connection": "test"})
        return names, result.content[0].text

    names, text = asyncio.run(go())
    assert names == {
        "list_connections", "model_health", "list_findings", "search_model", "get_object",
        "list_undocumented", "propose_description", "submit_for_review",
    }  # fmt: skip
    assert not any(w in n for n in names for w in ("approve", "apply", "rollback", "delete"))
    assert json.loads(text)["model"] == "Retail"
