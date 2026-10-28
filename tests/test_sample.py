from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from datawright.cli import app
from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi.health import health
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.rules import run_rules
from datawright.core import gitops
from datawright.fixtures import SAMPLES, make_sample


@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_opens_as_a_project(name: str) -> None:
    """Power BI Desktop opens Retail.pbip -> Retail.Report -> ../Retail.SemanticModel."""
    root = FIXTURES_DIR / "pbip" / name
    pbip = json.loads((root / "Retail.pbip").read_text(encoding="utf-8"))
    assert pbip["artifacts"] == [{"report": {"path": "Retail.Report"}}]
    pbir = json.loads((root / "Retail.Report" / "definition.pbir").read_text(encoding="utf-8"))
    assert pbir["datasetReference"]["byPath"]["path"] == "../Retail.SemanticModel"
    pages = root / "Retail.Report" / "definition" / "pages"
    order = json.loads((pages / "pages.json").read_text(encoding="utf-8"))["pageOrder"]
    assert order and all((pages / p / "page.json").exists() for p in order)


def test_csv_columns_match_what_the_queries_read() -> None:
    for variant in ("retail-clean", "retail-issues"):
        tables = FIXTURES_DIR / "pbip" / variant / "Retail.SemanticModel" / "definition" / "tables"
        for table, file in (("Sales", "sales"), ("Product", "product"), ("Customer", "customer")):
            m = re.search(r"Columns=(\d+)", (tables / f"{table}.tmdl").read_text(encoding="utf-8"))
            assert m is not None
            with (FIXTURES_DIR / "data" / f"{file}.csv").open(encoding="utf-8") as f:
                header = next(csv.reader(f))
            assert int(m.group(1)) <= len(header), (variant, table)


def test_blank_report_counts_as_no_report() -> None:
    model = next(iter(open_source(FIXTURES_DIR / "pbip" / "retail-clean").models().values()))
    assert model.report_usage is None and health(model).score == 100


def test_sample_points_the_model_at_its_data(tmp_path: Path) -> None:
    clean = make_sample("retail-clean", tmp_path / "clean")
    assert sorted(p.name for p in (clean / "data").iterdir()) == [
        "customer.csv", "product.csv", "sales.csv"
    ]  # fmt: skip
    expressions = (clean / "Retail.SemanticModel" / "definition" / "expressions.tmdl").read_text(
        encoding="utf-8"
    )
    assert str(clean / "data") in expressions and "IsParameterQuery=true" in expressions
    assert health(next(iter(open_source(clean).models().values()))).score == 100

    issues = make_sample("retail-issues", tmp_path / "issues")
    sales = (issues / "Retail.SemanticModel" / "definition" / "tables" / "Sales.tmdl").read_text(
        encoding="utf-8"
    )
    assert str(issues / "data" / "sales.csv") in sales
    rules = [f.rule for f in run_rules(open_source(issues).models().values())]
    assert rules.count("pq-hardcoded-local-path") == 3  # still hard-coded, just to real files

    with pytest.raises(ValueError, match="isn't empty"):
        make_sample("retail-clean", clean)
    with pytest.raises(ValueError, match="unknown sample"):
        make_sample("nope", tmp_path / "x")


def test_sample_cli_with_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(tmp_path / "data"))
    dest = tmp_path / "report"
    r = CliRunner().invoke(app, ["sample", "retail-report", str(dest), "--track"])
    assert r.exit_code == 0, r.output
    assert "Now tracking" in r.output and "Retail.pbip" in r.output
    assert gitops.project_root(dest) == dest.resolve()
