from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from datawright.cli import app
from datawright.config import FIXTURES_DIR, REPO_ROOT
from datawright.connectors.powerbi import ci
from datawright.connectors.powerbi.health import health
from datawright.connectors.powerbi.pbip import open_source

ISSUES = FIXTURES_DIR / "pbip" / "retail-issues"
CLEAN = FIXTURES_DIR / "pbip" / "retail-clean"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(tmp_path / "data"))


def test_locate_points_at_the_declaration() -> None:
    connector = open_source(ISSUES)
    sales = ISSUES / "Retail.SemanticModel" / "definition" / "tables" / "Sales.tmdl"
    lines = sales.read_text(encoding="utf-8").splitlines()
    path, line = ci.locate(connector, "Retail/Sales/measure/Margin %")  # type: ignore[misc]
    assert path == sales and lines[line - 1].startswith("\tmeasure 'Margin %' =")
    path, line = ci.locate(connector, "Retail/Sales")  # type: ignore[misc]
    assert lines[line - 1] == "table Sales"
    rel = ci.locate(connector, "Retail/relationship/7d1e0b2a-5c44-4f6e-8a10-1a2b3c4d5e02")
    assert rel is not None and rel[0].name == "relationships.tmdl" and rel[1] > 1
    part = ci.locate(connector, "Retail/Sales/partition/Sales")
    assert part is not None and part[1] > line
    pbix = open_source(FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix")
    assert ci.locate(pbix, "Revenue Opportunities/Calendar") is None


def test_sarif_shape() -> None:
    connector = open_source(ISSUES)
    reports = [health(m) for m in connector.models().values()]
    sarif = json.loads(ci.to_sarif(connector, reports))
    run = sarif["runs"][0]
    rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
    assert sarif["version"] == "2.1.0" and "use-divide-function" in rule_ids
    assert len(run["results"]) == len(reports[0].findings)
    located = [r for r in run["results"] if "locations" in r]
    assert located and all(r["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1
                           for r in located)  # fmt: skip
    assert {r["level"] for r in run["results"]} <= {"error", "warning", "note"}


def test_cli_formats_and_gates(tmp_path: Path) -> None:
    r = runner.invoke(app, ["check", str(CLEAN), "--fail-on", "info", "--min-score", "100"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["check", str(ISSUES), "--format", "github", "--fail-on", "error"])
    assert r.exit_code == 1 and "Quality gate failed" in r.output
    assert "::error file=" in r.output and ",line=" in r.output
    r = runner.invoke(app, ["check", str(ISSUES), "--min-score", "50"])
    assert r.exit_code == 1 and "below the minimum 50" in r.output
    out = tmp_path / "report.json"
    r = runner.invoke(app, ["check", str(ISSUES), "--format", "json", "--output", str(out)])
    assert r.exit_code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["models"][0]["score"] == 0 and data["findings"][0]["file"]
    r = runner.invoke(app, ["check", str(CLEAN), "--format", "yaml"])
    assert r.exit_code == 1


def test_example_workflow_is_valid_yaml() -> None:
    path = REPO_ROOT / "examples" / "github-actions" / "power-bi-checks.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = data["jobs"]["datawright"]["steps"]
    commands = " ".join(s.get("run", "") for s in steps)
    assert "datawright check" in commands and "datawright diff" in commands
