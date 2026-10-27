"""Model audit rules (Phase 0 set). Phase 1 ports the Best Practice Analyzer list."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from datawright.connectors.powerbi.tmdl import SemanticModel
from datawright.core.paths import make_path


@dataclass(frozen=True)
class RuleFinding:
    rule: str
    severity: str
    object_path: str
    message: str


Rule = Callable[[SemanticModel], Iterable[RuleFinding]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def has_division_operator(dax: str) -> bool:
    """True when `/` is used as an operator: not in strings, [names], 'tables' or comments."""
    i, n = 0, len(dax)
    while i < n:
        ch = dax[i]
        nxt = dax[i + 1] if i + 1 < n else ""
        if ch == '"':
            i += 1
            while i < n:
                if dax[i] == '"':
                    if i + 1 < n and dax[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
        elif ch == "[":
            close = dax.find("]", i + 1)
            i = n if close < 0 else close
        elif ch == "'":
            i += 1
            while i < n:
                if dax[i] == "'":
                    if i + 1 < n and dax[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif (ch == "/" and nxt == "/") or (ch == "-" and nxt == "-"):
            newline = dax.find("\n", i)
            i = n if newline < 0 else newline
        elif ch == "/" and nxt == "*":
            close = dax.find("*/", i + 2)
            i = n if close < 0 else close + 1
        elif ch == "/":
            return True
        i += 1
    return False


@rule
def measure_missing_description(model: SemanticModel) -> Iterable[RuleFinding]:
    for t in model.tables.values():
        for m in t.measures:
            if not (m.description or "").strip():
                yield RuleFinding(
                    "measure-missing-description",
                    "warning",
                    make_path(model.name, t.name, "measure", m.name),
                    f"Measure [{m.name}] has no description. People and Copilot can't tell what "
                    "it means.",
                )


@rule
def use_divide_function(model: SemanticModel) -> Iterable[RuleFinding]:
    for t in model.tables.values():
        for m in t.measures:
            if has_division_operator(m.expression):
                yield RuleFinding(
                    "use-divide-function",
                    "warning",
                    make_path(model.name, t.name, "measure", m.name),
                    f"Measure [{m.name}] divides with `/`. Use DIVIDE() so a zero or blank "
                    "denominator returns blank instead of an error.",
                )


@rule
def avoid_floating_point(model: SemanticModel) -> Iterable[RuleFinding]:
    for t in model.tables.values():
        for c in t.columns:
            if c.data_type == "double":
                yield RuleFinding(
                    "avoid-floating-point",
                    "warning",
                    make_path(model.name, t.name, "column", c.name),
                    f"Column {t.name}[{c.name}] is a floating-point number (double). Use fixed "
                    "decimal to avoid rounding errors in totals.",
                )


@rule
def avoid_bidirectional_relationship(model: SemanticModel) -> Iterable[RuleFinding]:
    for r in model.relationships:
        if r.cross_filtering == "bothDirections":
            yield RuleFinding(
                "avoid-bidirectional-relationship",
                "warning",
                make_path(model.name, "relationship", r.name),
                f"Relationship {r.from_column} → {r.to_column} filters in both directions. "
                "This can give ambiguous results and slow queries.",
            )


def run_rules(models: Iterable[SemanticModel]) -> list[RuleFinding]:
    out: list[RuleFinding] = []
    for model in models:
        for r in RULES:
            out.extend(r(model))
    return out
