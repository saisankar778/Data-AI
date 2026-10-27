from __future__ import annotations

import pytest

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi.dax import Ref, calls, code_only, references
from datawright.connectors.powerbi.health import health, score
from datawright.connectors.powerbi.model import (
    Column,
    Measure,
    Partition,
    Relationship,
    SemanticModel,
    Table,
)
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.rules import RULES, has_division_operator, run_rules


def findings(fixture: str) -> set[tuple[str, str]]:
    models = open_source(FIXTURES_DIR / "pbip" / fixture).models().values()
    return {(f.rule, f.object_path) for f in run_rules(models)}


def model(*tables: Table, relationships: list[Relationship] | None = None) -> SemanticModel:
    return SemanticModel("M", "pbip", {t.name: t for t in tables}, relationships or [])


def rules_on(m: SemanticModel) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in run_rules([m]):
        out.setdefault(f.rule, []).append(f.object_path)
    return out


def test_every_rule_has_metadata() -> None:
    ids = [info.id for info, _ in RULES]
    assert len(ids) == len(set(ids))
    for info, _ in RULES:
        assert info.severity in ("error", "warning", "info")
        assert info.category and info.title and info.why


def test_clean_model_has_no_findings_and_scores_100() -> None:
    assert findings("retail-clean") == set()
    m = next(iter(open_source(FIXTURES_DIR / "pbip" / "retail-clean").models().values()))
    report = health(m)
    assert report.score == 100 and report.ai_readiness == 100 and report.grade == "A"


def test_issues_model_scores_low() -> None:
    m = next(iter(open_source(FIXTURES_DIR / "pbip" / "retail-issues").models().values()))
    report = health(m)
    assert report.score < 40 and report.ai_readiness < 60
    assert report.findings[0].severity == "error"  # errors sort first


def test_score_weights() -> None:
    from datawright.connectors.powerbi.rules import RuleFinding

    fs = [RuleFinding("r", s, "p", "m") for s in ("error", "warning", "info", "info")]
    assert score(fs) == 92  # 100 - 5 - 2 - 0.5 - 0.5


# ---------------------------------------------------------------- rules not planted in fixtures


def test_error_prevention_rules() -> None:
    t = Table("T", columns=[
        Column("NoSource", "string", kind="data", source_column=None, description="x"),
        Column("Calc", "int64", kind="calculated", expression="", description="x"),
        Column("Bad\x01Name", "string", source_column="b", description="x"),
    ], measures=[Measure("Empty", "", format_string="0", description="x")])  # fmt: skip
    found = rules_on(model(t))
    assert found["data-column-needs-source"] == ["M/T/column/NoSource"]
    assert sorted(found["expression-required"]) == ["M/T/column/Calc", "M/T/measure/Empty"]
    assert found["invalid-name-characters"] == ["M/T/column/Bad\x01Name"]


def test_relationship_type_and_many_to_many() -> None:
    a = Table("A", columns=[Column("K", "string", source_column="K", is_hidden=True)])
    b = Table("B", columns=[Column("K", "int64", source_column="K", is_key=True)])
    rels = [
        Relationship("r1", "A", "K", "B", "K"),
        Relationship("r2", "A", "K", "B", "K", from_cardinality="many", to_cardinality="many",
                     cross_filtering="bothDirections"),
    ]  # fmt: skip
    found = rules_on(model(a, b, relationships=rels))
    assert sorted(found["relationship-same-data-type"]) == [
        "M/relationship/r1",
        "M/relationship/r2",
    ]
    assert found["many-to-many-single-direction"] == ["M/relationship/r2"]
    assert "avoid-bidirectional-relationship" not in found  # m2m rule covers it


def test_dax_pattern_rules() -> None:
    t = Table("T", columns=[
        Column("A", "decimal", source_column="A", summarize_by="none", description="x"),
        Column("Rel", "string", kind="calculated", expression="RELATED(U[Name])",
               description="x"),
    ], measures=[
        Measure("Treat", "CALCULATE([X], INTERSECT(VALUES(T[A]), VALUES(U[A])))", "x", "0"),
        Measure("Log", "EVALUATEANDLOG(SUM(T[A]))", "x", "0"),
        Measure("Pct", "1 - SUM(T[A]) / SUM(T[A])", "x", "0"),
        Measure("X", "SUM(T[A])", "x", "0"),
        Measure("Quoted", '"IFERROR(" & "INTERSECT("', "x", "0"),
    ])  # fmt: skip
    found = rules_on(model(t))
    assert found["use-treatas"] == ["M/T/measure/Treat"]
    assert found["no-evaluateandlog"] == ["M/T/measure/Log"]
    assert found["avoid-1-minus-x-over-y"] == ["M/T/measure/Pct"]
    assert found["calculated-column-uses-related"] == ["M/T/column/Rel"]
    assert "avoid-iferror" not in found  # function names inside strings don't count


def test_auto_date_and_generic_names() -> None:
    auto = Table("LocalDateTable_123", is_hidden=True,
                 columns=[Column("Date", "dateTime", source_column="Date")],
                 partitions=[Partition("p", "calculated", source="CALENDAR(1,2)")])  # fmt: skip
    t = Table("T", description="x", columns=[
        Column("Column1", "string", source_column="c", description="x"),
    ])  # fmt: skip
    found = rules_on(model(auto, t))
    assert found["auto-date-tables"] == ["M/LocalDateTable_123"]
    assert found["generic-column-name"] == ["M/T/column/Column1"]
    assert "unnecessary-hidden-columns" not in found  # auto-date tables are reported once


def test_field_parameters_are_not_unused() -> None:
    fp = Table("Pick", columns=[
        Column("Pick", "string", kind="calculatedTableColumn", description="x"),
        Column("Fields", "string", kind="calculatedTableColumn", is_hidden=True),
    ], partitions=[Partition("p", "calculated", source="{(\"A\", NAMEOF('T'[A]), 0)}")])  # fmt: skip  # noqa: E501
    found = rules_on(model(fp))
    assert "unnecessary-hidden-columns" not in found


def test_unused_hidden_table_is_one_finding() -> None:
    stage = Table("Stage", is_hidden=True, columns=[
        Column(n, "string", source_column=n) for n in ("A", "B", "C")
    ])  # fmt: skip
    found = rules_on(model(stage))
    assert found["unnecessary-hidden-columns"] == ["M/Stage"]


def test_parameters_are_not_hard_coded_paths() -> None:
    m = model(Table("T", description="x"))
    m.expressions["Folder"] = '"C:\\Data\\" meta [IsParameterQuery=true, Type="Text"]'
    m.expressions["Raw"] = 'let S = Csv.Document(File.Contents("D:\\x.csv")) in S'
    found = rules_on(m)
    assert found["pq-hardcoded-local-path"] == ["M/expression/Raw"]
    assert found["pq-untyped-source"] == ["M/expression/Raw"]


# ---------------------------------------------------------------- DAX scanner


@pytest.mark.parametrize(
    ("dax", "expected"),
    [
        ("[Profit] / [Total Sales]", True),
        ("1 /2", True),
        ("DIVIDE([Profit], [Total Sales])", False),
        ('"a/b"', False),
        ('"say ""a/b"""', False),
        ("[Sales/Cost]", False),
        ("'P/L'[Amount]", False),
        ("1 // a / b\n+ 2", False),
        ("1 -- a / b\n+ 2", False),
        ("/* a / b */ 1", False),
        ("/* a / b */ 1 / 2", True),
    ],
)
def test_division_operator_detection(dax: str, expected: bool) -> None:
    assert has_division_operator(dax) is expected


def test_references() -> None:
    dax = "CALCULATE([Total], 'Sales Table'[Col], Sales[Amount]) // [NotThis]\n+ \"[Nor]\""
    assert references(dax) == [
        Ref(None, "Total"),
        Ref("Sales Table", "Col"),
        Ref("Sales", "Amount"),
    ]
    assert references("[A]]B]") == [Ref(None, "A]B")]


def test_code_only_and_calls() -> None:
    assert "IFERROR" not in code_only('"IFERROR(" // IFERROR(\n')
    assert calls("iferror (1, 2)", "IFERROR")
    assert not calls("MYIFERROR(1)", "IFERROR")
