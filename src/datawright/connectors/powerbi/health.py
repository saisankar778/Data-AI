"""Health check: a score, AI-readiness and prioritised findings for one or more models."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from datawright.connectors.powerbi.model import SemanticModel
from datawright.connectors.powerbi.rules import RuleFinding, run_rules

WEIGHTS = {"error": 5.0, "warning": 2.0, "info": 0.5}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass
class HealthReport:
    model: str
    source: str
    score: int
    ai_readiness: int  # % of visible tables, columns and measures with a description
    documented: int
    visible: int
    tables: int
    columns: int
    measures: int
    findings: list[RuleFinding] = field(default_factory=list)

    @property
    def by_severity(self) -> Counter[str]:
        return Counter(f.severity for f in self.findings)

    @property
    def by_rule(self) -> Counter[str]:
        return Counter(f.rule for f in self.findings)

    @property
    def by_category(self) -> Counter[str]:
        return Counter(f.category for f in self.findings)

    @property
    def grade(self) -> str:
        for grade, floor in (("A", 90), ("B", 75), ("C", 60), ("D", 40)):
            if self.score >= floor:
                return grade
        return "E"


def score(findings: list[RuleFinding]) -> int:
    penalty = sum(WEIGHTS.get(f.severity, 0.0) for f in findings)
    return max(0, round(100 - penalty))


def ai_readiness(model: SemanticModel) -> tuple[int, int]:
    """(documented, visible) over visible tables, columns and measures; skips auto-date tables."""
    documented = visible = 0
    for t in model.tables.values():
        if t.is_hidden or t.is_auto_date:
            continue
        objs: list[tuple[bool, str | None]] = [(False, t.description)]
        objs += [(c.is_hidden, c.description) for c in t.columns]
        objs += [(m.is_hidden, m.description) for m in t.measures]
        for hidden, desc in objs:
            if not hidden:
                visible += 1
                documented += bool((desc or "").strip())
    return documented, visible


def _finding_order(f: RuleFinding) -> tuple[int, str, str]:
    return SEVERITY_ORDER.get(f.severity, 9), f.rule, f.object_path


def health(model: SemanticModel) -> HealthReport:
    findings = sorted(run_rules([model]), key=_finding_order)
    documented, visible = ai_readiness(model)
    return HealthReport(
        model=model.name,
        source=model.source,
        score=score(findings),
        ai_readiness=round(100 * documented / visible) if visible else 100,
        documented=documented,
        visible=visible,
        tables=sum(1 for t in model.tables.values() if not t.is_auto_date),
        columns=sum(len(t.columns) for t in model.tables.values() if not t.is_auto_date),
        measures=len(model.all_measures()),
        findings=findings,
    )
