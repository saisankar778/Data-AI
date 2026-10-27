from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from datawright.agents.ask import offline_script, run_ask
from datawright.agents.scripted import ScriptedClient, text, tool_calls
from datawright.config import Settings, load_env_file
from datawright.connectors.powerbi.search import search, terms
from datawright.models import Connection, User
from datawright.services import connector_for


def test_search_ranks_names_and_descriptions(conn: Connection) -> None:
    connector = connector_for(conn)
    assert search(connector, "profit margin")[0]["object_path"] == "Retail/Sales/measure/Margin %"
    assert search(connector, "unit price")[0]["object_path"] == "Retail/Sales/column/UnitPrice"
    assert search(connector, "the of and") == []
    assert terms("Which customers ordered?") == ["customer", "ordered"]


def test_ask_offline_cites_objects(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    run = run_ask(session, settings, ScriptedClient(offline_script), conn, user,
                  "Which measure gives margin?")  # fmt: skip
    assert run.status == "succeeded" and run.permission == "read"
    assert "Retail/Sales/measure/Margin %" in (run.final_text or "")
    assert [s.tool for s in run.steps if s.kind == "tool_call"][0] == "search_model"


def test_ask_has_only_read_tools(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    client = ScriptedClient([tool_calls(("list_relationships", {"table": "Sales"})), text("ok")])
    run_ask(session, settings, client, conn, user, "How does Sales relate to Calendar?")
    assert {t["name"] for t in client.calls[0]["tools"]} == {
        "search_model", "get_object", "list_relationships"
    }  # fmt: skip
    result = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Sales[ShipDate]" in result and "Calendar[Date]" in result


def test_ask_needs_a_question(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    with pytest.raises(ValueError):
        run_ask(session, settings, ScriptedClient([]), conn, user, "  ")


def test_env_file_never_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nexport DW_TEST_A=one\nDW_TEST_B = "two words"\nDW_TEST_C=three\nnot a line\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DW_TEST_C", "kept")
    monkeypatch.delenv("DW_TEST_A", raising=False)
    monkeypatch.delenv("DW_TEST_B", raising=False)
    loaded = load_env_file(env)
    assert loaded == ["DW_TEST_A", "DW_TEST_B"]
    assert os.environ["DW_TEST_A"] == "one" and os.environ["DW_TEST_B"] == "two words"
    assert os.environ["DW_TEST_C"] == "kept"
    for key in loaded:
        monkeypatch.delenv(key)
    assert load_env_file(tmp_path / "missing.env") == []
