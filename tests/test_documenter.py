from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.documenter import offline_script, run_documenter
from datawright.agents.scripted import ScriptedClient, text, tool_calls
from datawright.config import Settings
from datawright.models import AgentRun, ChangeSet, Connection, Finding, User


def change_set_of(session: Session, run: AgentRun) -> ChangeSet | None:
    return session.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))


def test_offline_run_proposes_a_validated_change_set(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    run = run_documenter(session, settings, ScriptedClient(offline_script(3)), conn, user, limit=3)
    assert run.status == "succeeded", run.error
    cs = change_set_of(session, run)
    assert cs is not None and cs.status == "proposed"
    assert sum(len(i.intents) for i in cs.items) == 3
    linked = session.scalars(select(Finding).where(Finding.change_item_id.is_not(None))).all()
    assert len(linked) == 3 and {f.rule for f in linked} == {"measure-missing-description"}
    assert run.steps[-1].kind == "check" and not run.steps[-1].is_error


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
            (
                "propose_description",
                {"object_path": "Retail/Sales/measure/Nope", "description": "x"},
            ),
            ("propose_description", {"object_path": path, "description": "Total sales."}),
            ("propose_description", {"object_path": path, "description": "Again."}),
            (
                "propose_description",
                {"object_path": "Retail/Sales/measure/Profit", "description": "Over the limit."},
            ),
        ),
        text("done"),
    ]
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
