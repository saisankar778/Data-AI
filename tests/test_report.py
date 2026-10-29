from __future__ import annotations

import json
from pathlib import Path

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi.diff import diff_revisions
from datawright.connectors.powerbi.model import Column, SemanticModel, Table
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.report import (
    ReportUsage,
    _legacy_layout,
    field_refs,
    pbix_report_usage,
)
from datawright.connectors.powerbi.rules import run_rules
from datawright.fixtures import make_fixture_repo

REPORT = FIXTURES_DIR / "pbip" / "retail-report"


def test_pbir_fixture_usage() -> None:
    model = next(iter(open_source(REPORT).models().values()))
    usage = model.report_usage
    assert usage is not None and usage.reports == ["Retail"]
    assert usage.used("Sales", "Margin %") and usage.used("customer", "city")  # case-insensitive
    assert not usage.used("Customer", "Segment") and not usage.used("Sales", "Order Lines")
    assert usage.where("Product", "Product Count") == ["Retail / Overview / multiRowCard"]


def test_models_without_reports_have_no_usage() -> None:
    model = next(iter(open_source(FIXTURES_DIR / "pbip" / "retail-clean").models().values()))
    assert model.report_usage is None
    assert not any(f.rule.startswith("unused-") for f in run_rules([model]))


def test_real_pbix_with_pbir_inside() -> None:
    usage = pbix_report_usage(FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix")
    assert usage is not None and len(usage.uses) >= 10
    assert usage.used("Calculations", "Opportunity Count")
    assert any("barChart" in w for w in usage.where("Account", "Region"))


def test_legacy_layout_aliases_and_string_configs() -> None:
    config = {
        "name": "v1",
        "singleVisual": {
            "visualType": "card",
            "prototypeQuery": {
                "From": [{"Name": "s", "Entity": "Sales"}],
                "Select": [{"Measure": {"Expression": {"SourceRef": {"Source": "s"}},
                                        "Property": "Total Sales"}}],
            },
        },
    }  # fmt: skip
    layout = {
        "sections": [{
            "displayName": "Page 1",
            "visualContainers": [{"config": json.dumps(config)}],
            "filters": json.dumps([{"expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": "Calendar"}}, "Property": "Year"}}}]),
        }],
    }  # fmt: skip
    usage = _legacy_layout(layout, "Old")
    assert usage.where("Sales", "Total Sales") == ["Old / Page 1 / card"]
    assert usage.where("Calendar", "Year") == ["Old / Page 1 / page filter"]
    assert list(field_refs("not json")) == []


def test_hidden_column_used_by_a_visual_is_not_unnecessary() -> None:
    t = Table("T", description="x", columns=[
        Column("Shown", "string", source_column="s", description="x"),
        Column("Hidden", "string", source_column="h", is_hidden=True),
    ])  # fmt: skip
    model = SemanticModel("M", "pbip", {"T": t}, [])
    assert {f.rule for f in run_rules([model])} >= {"unnecessary-hidden-columns"}
    model.report_usage = ReportUsage(["R"])
    model.report_usage.add("T", "Hidden", "R / P / table")
    model.report_usage.add("T", "Shown", "R / P / table")
    rules = {f.rule for f in run_rules([model])}
    assert "unnecessary-hidden-columns" not in rules and "unused-column" not in rules


def test_diff_reports_broken_visuals(tmp_path: Path) -> None:
    repo = make_fixture_repo("retail-report", tmp_path / "rr")
    sales = repo / "Retail.SemanticModel" / "definition" / "tables" / "Customer.tmdl"
    text = sales.read_bytes().decode("utf-8")
    assert "\tcolumn City\n" in text
    sales.write_bytes(text.replace("\tcolumn City\n", "\tcolumn Town\n", 1).encode("utf-8"))
    [d] = diff_revisions(repo, "HEAD", None)
    assert d.broken_visuals == [
        "column Retail/Customer/column/City is used by Retail / Details / clusteredColumnChart"
    ]
