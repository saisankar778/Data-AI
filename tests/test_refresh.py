from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from datawright.agents.refresh_doctor import diagnose_connection, explain, query_code
from datawright.agents.scripted import ScriptedClient, text, tool_calls
from datawright.config import FIXTURES_DIR, Settings
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.refresh import PATTERNS, diagnose, m_steps
from datawright.mcp_server import DatawrightTools
from datawright.models import Connection, User

Q = chr(39)  # a single quote, to keep error strings readable below
MISSING_FILE = f"DataSource.Error: Could not find file {Q}C:\\Data\\sales.csv{Q}."


def test_pattern_ids_are_unique() -> None:
    ids = [p.id for p in PATTERNS]
    assert len(ids) == len(set(ids)) >= 15


def test_m_steps_handles_quoted_names() -> None:
    m = (
        'let\n    Source = Sql.Database("srv", "db"),\n'
        '    #"Changed Type" = Table.TransformColumnTypes(Source, {{"A", Int64.Type}}),\n'
        '    #"Say ""hi""" = Table.RenameColumns(#"Changed Type", {{"A", "B"}})\nin\n'
        '    #"Say ""hi"""'
    )
    names = [s for s, _ in m_steps(m)]
    assert names == ["Source", "Changed Type", 'Say "hi"']


def test_no_match_returns_nothing() -> None:
    models = list(open_source(FIXTURES_DIR / "pbip" / "retail-clean").models().values())
    assert diagnose("Something completely different happened.", models) == []


def test_query_code(conn: Connection) -> None:
    from datawright.services import connector_for

    q = query_code(connector_for(conn), "Retail/Sales/partition/Sales")
    assert [s["step"] for s in q["steps"]] == ["Source", "Promoted"]
    with pytest.raises(KeyError):
        query_code(connector_for(conn), "Retail/Sales/partition/Nope")


def test_explain_uses_read_only_tools(
    session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    diagnoses = diagnose_connection(conn, MISSING_FILE)
    client = ScriptedClient([
        tool_calls(("get_query", {"object_path": "Retail/Sales/partition/Sales"})),
        text("What happened: the file moved. Fix: use a DataFolder parameter."),
    ])  # fmt: skip
    run = explain(session, settings, client, conn, user, MISSING_FILE, diagnoses)
    assert run.status == "succeeded" and "DataFolder" in (run.final_text or "")
    tools = {t["name"] for t in client.calls[0]["tools"]}
    assert tools == {"search_model", "get_object", "list_relationships", "get_query"}
    assert "file-not-found" in client.calls[0]["messages"][0]["content"]
    result = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Csv.Document" in result


def test_cli_web_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings,
    sessions: sessionmaker[Session], conn: Connection,
) -> None:  # fmt: skip
    from datawright.cli import app
    from datawright.web.app import create_app

    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()
    r = runner.invoke(app, ["diagnose", "test", MISSING_FILE])
    assert r.exit_code == 0, r.output
    assert "[file-not-found]" in r.output and "Retail/Sales/partition/Sales" in r.output
    log = tmp_path / "refresh.log"
    log.write_text("Formula.Firewall: Query 'Sales' references other queries", encoding="utf-8")
    folder = str(FIXTURES_DIR / "pbip" / "retail-clean")
    r = runner.invoke(app, ["diagnose", folder, "--file", str(log)])
    assert r.exit_code == 0 and "[formula-firewall]" in r.output
    r = runner.invoke(app, ["diagnose", "test", "nothing known"])
    assert "No known refresh-error pattern matched" in r.output

    client = TestClient(create_app(settings, sessions, allowed_hosts={"testserver"}))
    page = client.get("/diagnose").text
    token = page.split('name="csrf" value="')[1].split('"')[0]
    resp = client.post("/diagnose", data={"csrf": token, "connection": "test",
                                          "error": MISSING_FILE})  # fmt: skip
    assert "A file the query reads" in resp.text and "Retail/Sales/partition/Sales" in resp.text
    assert client.post("/diagnose", data={"csrf": "x"}).status_code == 403

    tools = DatawrightTools(settings, sessions)
    [d] = tools.diagnose_refresh_error("test", MISSING_FILE)
    assert d["pattern"] == "file-not-found" and d["evidence"]
    assert tools.get_query("test", "Retail/Sales/partition/Sales")["steps"]
