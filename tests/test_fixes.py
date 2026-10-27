from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from datawright.config import FIXTURES_DIR, Settings
from datawright.connectors.powerbi import tmdl
from datawright.connectors.powerbi.fixes import (
    AUTO_FIXABLE,
    GUIDANCE,
    data_category_for,
    format_string_for,
    plan_fixes,
)
from datawright.connectors.powerbi.model import Column
from datawright.connectors.powerbi.pbip import PbipRepoConnector, open_source
from datawright.connectors.powerbi.rules import RULES, run_rules
from datawright.core.changesets import ChangeSetService
from datawright.fixing import propose_fixes
from datawright.mcp_server import DatawrightTools
from datawright.models import Connection, Finding, User
from datawright.services import connect

S = "Retail/Sales/column"
EXPECTED_FIXES = {
    ("mark-date-table", "Retail/Calendar"),
    ("use-divide-function", "Retail/Sales/measure/Margin %"),
    ("measure-format-string", "Retail/Sales/measure/Avg Order Value"),
    *{("numeric-column-summarize-by", f"{S}/{c}")
      for c in ("ProductKey", "CustomerKey", "Quantity", "UnitPrice", "Amount", "Cost")},
    *{("hide-foreign-keys", f"{S}/{c}") for c in ("ProductKey", "CustomerKey", "OrderDate",
                                                 "ShipDate")},
    ("mark-primary-keys", "Retail/Product/column/ProductKey"),
    ("mark-primary-keys", "Retail/Customer/column/CustomerKey"),
    ("sort-month-columns", "Retail/Calendar/column/Month"),
    ("add-data-category", "Retail/Customer/column/City"),
}  # fmt: skip


def issues() -> PbipRepoConnector:
    c = open_source(FIXTURES_DIR / "pbip" / "retail-issues")
    assert isinstance(c, PbipRepoConnector)
    return c


def test_every_rule_has_guidance() -> None:
    assert {info.id for info, _ in RULES} == set(GUIDANCE)
    assert AUTO_FIXABLE <= set(GUIDANCE)


def test_plan_on_issues_fixture() -> None:
    connector = issues()
    fixes, manual = plan_fixes(connector, run_rules(connector.models().values()))
    assert {(f.rule, f.object_path) for f in fixes} == EXPECTED_FIXES
    manual_rules = {m.rule for m in manual}
    assert "model-has-date-table" not in manual_rules  # solved by marking Calendar
    assert "use-divide-function" in manual_rules  # Sales YoY % is multi-line: needs a person
    margin = next(f for f in fixes if f.rule == "use-divide-function")
    assert margin.intents[0]["value"] == "DIVIDE([Profit], [Total Sales])"


def test_applied_fixes_remove_exactly_their_findings(tmp_path: Path) -> None:
    """Simulate merging the fix branch: findings fixed, nothing new introduced."""
    connector = issues()
    before = {(f.rule, f.object_path) for f in run_rules(connector.models().values())}
    fixes, _ = plan_fixes(connector, run_rules(connector.models().values()))
    work = tmp_path / "work"
    shutil.copytree(FIXTURES_DIR / "pbip" / "retail-issues", work)
    for fix in fixes:
        path = work / fix.file_path
        text = tmdl.read_text(path)
        for intent in fix.intents:
            text = {
                "tmdl.set_property": tmdl.apply_set_property,
                "tmdl.set_expression": tmdl.apply_set_expression,
            }[intent["op"]](text, intent)
        path.write_bytes(text.encode("utf-8"))
    after = {(f.rule, f.object_path) for f in run_rules(open_source(work).models().values())}
    assert after <= before, f"fixes introduced new findings: {after - before}"
    fixed = before - after
    assert EXPECTED_FIXES <= fixed
    assert ("model-has-date-table", "Retail") in fixed


def test_set_property_edits() -> None:
    text = "table T\n\tlineageTag: x\n\n\tcolumn A\n\t\tdataType: int64\n\t\tsummarizeBy: sum\n"
    out = tmdl.set_property(text, "column", "A", "summarizeBy", "none", table="T")
    assert out == text.replace("summarizeBy: sum", "summarizeBy: none")
    out = tmdl.set_property(text, "column", "A", "isHidden", True, table="T")
    assert out == text.replace("\t\tdataType: int64\n", "\t\tisHidden\n\t\tdataType: int64\n")
    assert tmdl.set_property(out, "column", "A", "isHidden", None, table="T") == text
    out = tmdl.set_property(text, "table", "T", "dataCategory", "Time")
    assert out.startswith("table T\n\tdataCategory: Time\n\tlineageTag: x\n")
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_property(text, "column", "A", "summarizeBy", "two\nlines", table="T")


def test_set_expression_single_line_only() -> None:
    text = "table T\n\tmeasure 'M %' = [A] / [B]\n\t\tformatString: 0%\n\tmeasure N =\n\t\t\t1\n"
    out = tmdl.set_expression(text, "measure", "M %", "DIVIDE([A], [B])", table="T")
    assert "\tmeasure 'M %' = DIVIDE([A], [B])\n\t\tformatString: 0%\n" in out
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_expression(text, "measure", "N", "2", table="T")
    part = "table T\n\tpartition T = m\n\t\tsource =\n\t\t\t\tlet x = 1 in x\n"
    with pytest.raises(tmdl.TmdlError):
        tmdl.set_property(part, "partition", "T", "source", "x", table="T")


def test_heuristics() -> None:
    assert format_string_for("Margin %", "[a]") == "0.0%"
    assert format_string_for("Orders", "DISTINCTCOUNT(S[O])") == "#,0"
    assert format_string_for("Avg Order Value", "DIVIDE([S], DISTINCTCOUNT(S[O]))") == "#,0.00"
    assert data_category_for(Column("Country Name", "string")) == "Country"
    assert data_category_for(Column("Latitude", "decimal")) == "Latitude"
    assert data_category_for(Column("City Code", "int64")) is None


def test_propose_fixes_service_and_review_by_edit(
    session: Session, user: User, conn: Connection
) -> None:
    result = propose_fixes(session, conn, user)
    cs = result.change_set
    assert cs is not None and cs.status == "proposed"
    assert len(result.fixes) == len(EXPECTED_FIXES) and not result.skipped
    linked = session.scalars(select(Finding).where(Finding.change_item_id.is_not(None))).all()
    assert {(f.rule, f.object_path) for f in linked} >= EXPECTED_FIXES
    sales = next(i for i in cs.items if i.file_path.endswith("Sales.tmdl"))
    key_edits = [i for i in sales.intents if i["object_path"] == f"{S}/ProductKey"]
    assert {i["key"] for i in key_edits} == {"summarizeBy", "isHidden"}
    hide = next(i for i in key_edits if i["key"] == "isHidden")
    ChangeSetService(session, user).approve(cs, decisions={hide["id"]: {"accept": False}})
    sales = next(i for i in cs.items if i.file_path.endswith("Sales.tmdl"))
    kept = [i for i in sales.intents if i["object_path"] == f"{S}/ProductKey"]
    assert [i["key"] for i in kept] == ["summarizeBy"]  # same object, decided separately
    assert cs.rejected_count == 1


def test_pbix_and_read_only_are_refused(session: Session, user: User, conn: Connection) -> None:
    pbix = connect(session, user, "pbix", FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix")
    with pytest.raises(ValueError, match="Save as"):
        propose_fixes(session, pbix, user)
    conn.capability = "read"
    with pytest.raises(ValueError, match="read-only"):
        propose_fixes(session, conn, user)


def test_fix_from_cli_web_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: Path, settings: Settings,
    sessions: sessionmaker[Session], conn: Connection,
) -> None:  # fmt: skip
    from datawright.cli import app
    from datawright.web.app import create_app

    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()
    r = runner.invoke(app, ["fix", "test", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "Would fix 17 findings" in r.output and "need a person" in r.output
    client = TestClient(create_app(settings, sessions, allowed_hosts={"testserver"}))
    page = client.get(f"/connections/{conn.id}").text
    assert "Propose automatic fixes" in page and "auto-fix" in page
    token = page.split('name="csrf" value="')[1].split('"')[0]
    resp = client.post(f"/connections/{conn.id}/fix", data={"csrf": token}, follow_redirects=False)
    assert resp.status_code == 303 and "/change-sets/" in resp.headers["location"]
    assert "fixes numeric-column-summarize-by" in client.get(resp.headers["location"]).text
    out = DatawrightTools(settings, sessions).propose_fixes("test")
    assert out["change_set"] is not None and out["needs_a_person"]
