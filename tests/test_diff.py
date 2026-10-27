from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from datawright.config import Settings
from datawright.connectors.powerbi.diff import diff_revisions, models_at, render_markdown
from datawright.connectors.powerbi.model import Column, Measure, SemanticModel, Table
from datawright.connectors.powerbi.rules import run_rules
from datawright.core import gitops
from datawright.fixing import propose_fixes
from datawright.fixtures import make_fixture_repo
from datawright.models import Connection, User

SALES = "Retail.SemanticModel/definition/tables/Sales.tmdl"
REL = "Retail.SemanticModel/definition/relationships.tmdl"


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    return make_fixture_repo("retail-clean", tmp_path / "clean")


def edit(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    text = path.read_bytes().decode("utf-8")
    assert old in text
    path.write_bytes(text.replace(old, new, 1).encode("utf-8"))


def test_rename_breaks_references(clean_repo: Path) -> None:
    edit(clean_repo, SALES, "\tcolumn Quantity\n", "\tcolumn Qty\n")
    edit(clean_repo, SALES, "measure Profit = [Total Sales] - [Total Cost]",
         "measure Profit = [Total Sales] - [Total Cost] - 1")  # fmt: skip
    edit(clean_repo, REL, "\tisActive: false\n", "")
    [d] = diff_revisions(clean_repo, "HEAD", None)
    changes = {(c.change, c.kind, c.object_path) for c in d.changes}
    assert ("removed", "column", "Retail/Sales/column/Quantity") in changes
    assert ("added", "column", "Retail/Sales/column/Qty") in changes
    profit = next(c for c in d.changes if c.object_path == "Retail/Sales/measure/Profit")
    assert profit.fields == [("expression", "[Total Sales] - [Total Cost]",
                              "[Total Sales] - [Total Cost] - 1")]  # fmt: skip
    rel = next(c for c in d.changes if c.kind == "relationship")
    assert rel.fields == [("is_active", False, True)]
    assert [f.object_path for f in d.broken_references] == ["Retail/Sales/measure/Units"]
    assert d.score_delta < 0 and d.old_health and d.old_health.score == 100
    text = render_markdown([d])
    assert "Breaks 1 reference" in text and "Sales[Quantity]" in text


def test_models_at_a_revision(clean_repo: Path) -> None:
    edit(clean_repo, SALES, "\tcolumn Quantity\n", "\tcolumn Qty\n")
    gitops.git(clean_repo, "commit", "-qam", "rename")
    head = models_at(clean_repo, "HEAD")["Retail"].tables["Sales"]
    first = models_at(clean_repo, "HEAD~1")["Retail"].tables["Sales"]
    assert head.column("Qty") and not head.column("Quantity")
    assert first.column("Quantity") and not first.column("Qty")
    [d] = diff_revisions(clean_repo, "HEAD~1", "HEAD")
    assert d.broken_references


def test_unknown_reference_rule() -> None:
    t = Table("T", description="x", columns=[
        Column("A", "decimal", source_column="A", summarize_by="none", description="x"),
    ], measures=[
        Measure("Ok", "SUMX(ADDCOLUMNS(T, \"Doubled\", T[A] * 2), [Doubled])", "x", "0"),
        Measure("Series", "MAXX(GENERATESERIES(1, 5), [Value])", "x", "0"),
        Measure("Var", "VAR t = T RETURN SUMX(t, [A])", "x", "0"),
        Measure("Bad", "SUM(T[Missing]) + [Nope]", "x", "0"),
        Measure("OtherTable", "SUM(NotATable[X])", "x", "0"),
    ])  # fmt: skip
    m = SemanticModel("M", "pbip", {"T": t}, [])
    bad = [f for f in run_rules([m]) if f.rule == "unknown-reference"]
    assert [f.object_path for f in bad] == ["M/T/measure/Bad"]
    assert "T[Missing]" in bad[0].message and "[Nope]" in bad[0].message


def test_cli_diff(clean_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datawright.cli import app

    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    r = runner.invoke(app, ["diff", str(clean_repo)])
    assert r.exit_code == 0 and "No model changes." in r.output
    edit(clean_repo, SALES, "\tcolumn Quantity\n", "\tcolumn Qty\n")
    r = runner.invoke(app, ["diff", str(clean_repo), "--fail-on-break"])
    assert r.exit_code == 1 and "Breaks 1 reference" in r.output
    out = tmp_path / "pr.md"
    r = runner.invoke(app, ["diff", str(clean_repo), "--out", str(out)])
    assert r.exit_code == 0 and out.read_text(encoding="utf-8").startswith("## Model changes")


def test_change_set_page_previews_the_effect(
    session: Session, user: User, conn: Connection, settings: Settings,
    sessions: sessionmaker[Session],
) -> None:  # fmt: skip
    from datawright.web.app import create_app

    cs = propose_fixes(session, conn, user).change_set
    assert cs is not None
    client = TestClient(create_app(settings, sessions, allowed_hosts={"testserver"}))
    page = client.get(f"/change-sets/{cs.id}").text
    assert "What this changes" in page and "findings resolved" in page
    assert "This breaks references" not in page
