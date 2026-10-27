from __future__ import annotations

import pytest

from datawright.config import FIXTURES_DIR
from datawright.connectors.powerbi.pbip import PbipRepoConnector
from datawright.connectors.powerbi.rules import has_division_operator, run_rules


def findings(fixture: str) -> set[tuple[str, str]]:
    models = PbipRepoConnector(FIXTURES_DIR / "pbip" / fixture).models().values()
    return {(f.rule, f.object_path) for f in run_rules(models)}


def test_clean_model_has_no_findings() -> None:
    assert findings("retail-clean") == set()


def test_issues_model_has_each_planted_problem() -> None:
    found = findings("retail-issues")
    rules = sorted({r for r, _ in found})
    assert rules == [
        "avoid-bidirectional-relationship",
        "avoid-floating-point",
        "measure-missing-description",
        "use-divide-function",
    ]
    assert ("use-divide-function", "Retail/Sales/measure/Margin %") in found
    assert ("use-divide-function", "Retail/Sales/measure/Avg Order Value") not in found
    assert len([r for r, _ in found if r == "measure-missing-description"]) == 8


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
