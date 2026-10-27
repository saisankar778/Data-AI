from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.documenter import offline_script, run_documenter, undocumented
from datawright.agents.scripted import ScriptedClient, text, tool_calls
from datawright.config import Settings
from datawright.models import AgentRun, ChangeSet, Connection, Finding, User
from datawright.services import connector_for


def change_set_of(session: Session, run: AgentRun) -> ChangeSet | None:
    return session.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))


def test_undocumented_lists_measures_first(conn: Connection) -> None:
    refs = undocumented(connector_for(conn), ("measure", "column", "table"))
    kinds = [r.kind for r in refs]
    assert kinds == sorted(kinds, key=["measure", "column", "table"].index)
    assert len(refs) == 21  # 11 measures, 9 columns, 1 table in retail-issues
    assert all(not r.is_hidden for r in refs)


def test_offline_run_documents_all_kinds(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    client = ScriptedClient(offline_script(25))
    run = run_documenter(session, settings, client, conn, user, limit=25)
    assert run.status == "succeeded", run.error
    cs = change_set_of(session, run)
    assert cs is not None and cs.status == "proposed"
    intents = [i for item in cs.items for i in item.intents]
    assert len(intents) == 21
    assert {i["kind"] for i in intents} == {"measure", "column", "table"}
    linked = session.scalars(select(Finding).where(Finding.change_item_id.is_not(None))).all()
    assert len(linked) == 21
    assert {f.rule for f in linked} == {"measure-missing-description", "object-missing-description"}
    assert run.steps[-1].kind == "check" and not run.steps[-1].is_error


def test_kinds_filter(session: Session, settings: Settings, user: User, conn: Connection) -> None:
    run = run_documenter(
        session, settings, ScriptedClient(offline_script(25)), conn, user, limit=25,
        kinds=["column"],
    )  # fmt: skip
    cs = change_set_of(session, run)
    assert cs is not None
    assert {i["kind"] for item in cs.items for i in item.intents} == {"column"}


def test_read_only_connection_gets_no_change_set(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    conn.capability = "read"
    run = run_documenter(session, settings, ScriptedClient(offline_script(3)), conn, user, limit=3)
    assert run.status == "succeeded" and run.permission == "read"
    assert change_set_of(session, run) is None


def test_proposal_guards(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    path = "Retail/Sales/measure/Total Sales"
    script = [
        tool_calls(
            ("propose_description", {"object_path": path, "description": "x" * 301}),
            ("propose_description", {"object_path": "Retail/Sales/measure/Nope",
                                     "description": "x"}),
            ("propose_description", {"object_path": path, "description": "Total sales."}),
            ("propose_description", {"object_path": path, "description": "Again."}),
            ("propose_description", {"object_path": "Retail/Sales/measure/Profit",
                                     "description": "Over the limit."}),
        ),
        text("done"),
    ]  # fmt: skip
    client = ScriptedClient(script)
    run = run_documenter(session, settings, client, conn, user, limit=1)
    results = client.calls[1]["messages"][-1]["content"]
    assert [bool(r.get("is_error")) for r in results] == [True, True, False, True, True]
    assert "limit is 300" in results[0]["content"]
    assert "not found" in results[1]["content"]
    assert "already proposed" in results[3]["content"]
    assert "limit of 1" in results[4]["content"]
    cs = change_set_of(session, run)
    assert cs is not None and cs.status == "proposed"
    assert cs.items[0].intents[0]["value"] == "Total sales."


def test_get_object_payloads(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    script = [
        tool_calls(
            ("get_object", {"object_path": "Retail/Sales/column/ShipDate"}),
            ("get_object", {"object_path": "Retail/Sales"}),
        ),
        text("done"),
    ]  # fmt: skip
    client = ScriptedClient(script)
    run_documenter(session, settings, client, conn, user, limit=1)
    column, table = client.calls[1]["messages"][-1]["content"]
    assert '"relationships"' in column["content"] and "Calendar[Date]" in column["content"]
    assert '"power_query_functions"' in table["content"] and "Csv.Document" in table["content"]
    assert "C:\\\\Data" not in table["content"]  # literal paths stay out of prompts
