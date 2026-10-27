"""Model audit rules.

Most rules port Microsoft's Best Practice Analyzer (BPA) rule set (MIT licence, see NOTICE),
re-implemented in Python over our source-neutral model. Severity follows BPA (3 = error,
2 = warning, 1 = info), except four style rules lowered to warning so the health score
reflects risk rather than style: DAX_COLUMNS_FULLY_QUALIFIED, DAX_MEASURES_UNQUALIFIED,
PROVIDE_FORMAT_STRING_FOR_MEASURES and NUMERIC_COLUMN_SUMMARIZE_BY.
Rules without a `bpa` id are Datawright's own (Power Query refresh risks, AI readiness).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from datawright.connectors.powerbi.dax import (
    Ref,
    calls,
    code_only,
    has_division_operator,
    normalized,
    references,
)
from datawright.connectors.powerbi.model import Column, SemanticModel, Table
from datawright.core.paths import make_path

__all__ = ["RULES", "RuleFinding", "RuleInfo", "has_division_operator", "run_rules"]


@dataclass(frozen=True)
class RuleInfo:
    id: str
    title: str
    category: str
    severity: str  # error | warning | info
    why: str
    bpa: str | None = None


@dataclass(frozen=True)
class RuleFinding:
    rule: str
    severity: str
    object_path: str
    message: str
    category: str = ""


@dataclass
class ModelIndex:
    """Lookups the rules share, built once per model."""

    model: SemanticModel
    measures: dict[str, tuple[Table, str]] = field(
        default_factory=dict
    )  # casefold -> (table, name)
    columns: set[tuple[str, str]] = field(default_factory=set)  # (table, column) casefold
    column_names: set[str] = field(default_factory=set)  # casefold
    referenced_columns: set[tuple[str | None, str]] = field(default_factory=set)
    referenced_measures: set[str] = field(default_factory=set)
    relationship_columns: set[tuple[str, str]] = field(default_factory=set)
    sort_by_columns: set[tuple[str, str]] = field(default_factory=set)
    hierarchy_columns: set[tuple[str, str]] = field(default_factory=set)
    activated: set[frozenset[tuple[str, str]]] = field(default_factory=set)

    @classmethod
    def build(cls, model: SemanticModel) -> ModelIndex:
        ix = cls(model)
        for t, m in model.all_measures():
            ix.measures[m.name.casefold()] = (t, m.name)
        for t, c in model.all_columns():
            ix.columns.add((t.name.casefold(), c.name.casefold()))
            ix.column_names.add(c.name.casefold())
            if c.sort_by_column:
                ix.sort_by_columns.add((t.name.casefold(), c.sort_by_column.casefold()))
        for t in model.tables.values():
            for h in t.hierarchies:
                for level in h.levels:
                    ix.hierarchy_columns.add((t.name.casefold(), level.casefold()))
        for r in model.relationships:
            ix.relationship_columns.add((r.from_table.casefold(), r.from_column.casefold()))
            ix.relationship_columns.add((r.to_table.casefold(), r.to_column.casefold()))
        for expr in ix.expressions():
            for ref in references(expr):
                name = ref.name.casefold()
                table = ref.table.casefold() if ref.table else None
                if table is None and name in ix.measures:
                    ix.referenced_measures.add(name)
                elif table is not None and (table, name) not in ix.columns and name in ix.measures:
                    ix.referenced_measures.add(name)
                else:
                    ix.referenced_columns.add((table, name))
            for a, b in _userelationship_pairs(expr):
                ix.activated.add(frozenset((a, b)))
        return ix

    def expressions(self) -> Iterator[str]:
        for _, m in self.model.all_measures():
            yield m.expression
        for _, c in self.model.all_columns():
            if c.expression:
                yield c.expression
        for t in self.model.tables.values():
            for p in t.partitions:
                if p.kind == "calculated" and p.source:
                    yield p.source

    def is_column_referenced(self, table: str, column: str) -> bool:
        t, c = table.casefold(), column.casefold()
        return (t, c) in self.referenced_columns or (None, c) in self.referenced_columns

    def is_measure(self, ref: Ref) -> bool:
        name = ref.name.casefold()
        if ref.table is None:
            return name in self.measures and name not in self.column_names
        return (ref.table.casefold(), name) not in self.columns and name in self.measures


def _userelationship_pairs(expr: str) -> Iterator[tuple[tuple[str, str], tuple[str, str]]]:
    code = code_only(expr)
    for m in re.finditer(r"USERELATIONSHIP\s*\(", code, re.I):
        args = references(expr[m.end() :])[:2]
        if len(args) == 2 and all(a.table for a in args):
            yield (
                ((args[0].table or "").casefold(), args[0].name.casefold()),
                ((args[1].table or "").casefold(), args[1].name.casefold()),
            )


Check = Callable[[SemanticModel, ModelIndex], Iterable[tuple[str, str]]]  # (object_path, message)
RULES: list[tuple[RuleInfo, Check]] = []


def rule(
    id: str, title: str, category: str, severity: str, why: str, bpa: str | None = None
) -> Callable[[Check], Check]:
    def register(fn: Check) -> Check:
        RULES.append((RuleInfo(id, title, category, severity, why, bpa), fn))
        return fn

    return register


def _mpath(model: SemanticModel, t: Table, name: str) -> str:
    return make_path(model.name, t.name, "measure", name)


def _cpath(model: SemanticModel, t: Table, c: Column) -> str:
    return make_path(model.name, t.name, "column", c.name)


def _visible(t: Table, hidden: bool) -> bool:
    return not hidden and not t.is_hidden


NUMERIC = {"int64", "decimal", "double"}


# ---------------------------------------------------------------- Performance


@rule(
    "avoid-floating-point",
    "Do not use floating point data types",
    "Performance",
    "warning",
    "Double can cause rounding errors in totals; use fixed decimal or whole number.",
    bpa="AVOID_FLOATING_POINT_DATA_TYPES",
)
def _floating(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if c.data_type == "double" and not t.is_auto_date:
            yield _cpath(model, t, c), f"{t.name}[{c.name}] is a floating-point number (double)."


@rule(
    "mark-date-table",
    "Date/calendar tables should be marked as a date table",
    "Performance",
    "warning",
    "Time intelligence needs a table marked as a date table with a date key.",
    bpa="DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE",
)
def _mark_date(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t in model.tables.values():
        if t.is_auto_date or not re.search(r"date|calendar", t.name, re.I):
            continue
        has_key = any(c.is_key and c.data_type == "dateTime" for c in t.columns)
        if t.data_category != "Time" or not has_key:
            yield (
                make_path(model.name, t.name),
                f"Table {t.name} looks like a date table but "
                "isn't marked as one (Mark as date table, with a date key column).",
            )


@rule(
    "auto-date-tables",
    "Remove auto-date tables",
    "Performance",
    "warning",
    "Auto-date tables add a hidden table per date column and waste memory.",
    bpa="REMOVE_AUTO-DATE_TABLE",
)
def _auto_date_tables(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t in model.tables.values():
        if t.is_auto_date:
            yield make_path(model.name, t.name), f"{t.name} is an auto-date table."


@rule(
    "auto-date-time-on",
    "Turn off auto date/time",
    "Performance",
    "warning",
    "With auto date/time on, Desktop creates a hidden date table for every date column.",
)
def _auto_date_setting(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    if model.annotations.get("__PBI_TimeIntelligenceEnabled") == "1":
        yield make_path(model.name), "Auto date/time is on (Options > Current file > Data load)."


@rule(
    "model-has-date-table",
    "Model should have a date table",
    "Performance",
    "warning",
    "Without a marked date table, time intelligence and date filtering are unreliable.",
    bpa="MODEL_SHOULD_HAVE_A_DATE_TABLE",
)
def _has_date_table(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    ok = any(
        t.data_category == "Time" and any(c.is_key and c.data_type == "dateTime" for c in t.columns)
        for t in model.tables.values()
    )
    if not ok:
        yield make_path(model.name), "The model has no table marked as a date table."


@rule(
    "many-to-many-single-direction",
    "Many-to-many relationships should be single-direction",
    "Performance",
    "warning",
    "Bi-directional many-to-many filtering is slow and ambiguous.",
    bpa="MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION",
)
def _m2m(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for r in model.relationships:
        if (
            r.from_cardinality == "many"
            and r.to_cardinality == "many"
            and r.cross_filtering == "bothDirections"
        ):
            yield (
                make_path(model.name, "relationship", r.name),
                f"Many-to-many relationship {r.label} filters in both directions.",
            )


@rule(
    "avoid-bidirectional-relationship",
    "Avoid bi-directional relationships",
    "Performance",
    "warning",
    "Filtering in both directions can give ambiguous results and slow queries.",
)
def _bidi(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for r in model.relationships:
        if r.cross_filtering == "bothDirections" and not (
            r.from_cardinality == "many" and r.to_cardinality == "many"
        ):
            yield (
                make_path(model.name, "relationship", r.name),
                f"Relationship {r.label} filters in both directions.",
            )


@rule(
    "calculated-column-uses-related",
    "Avoid calculated columns that use RELATED",
    "Performance",
    "warning",
    "Calculated columns compress poorly; a RELATED lookup is usually better in Power Query.",
    bpa="REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION",
)
def _calc_related(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if c.kind == "calculated" and c.expression and calls(c.expression, "RELATED"):
            yield _cpath(model, t, c), f"Calculated column {t.name}[{c.name}] uses RELATED."


# ---------------------------------------------------------------- DAX


@rule(
    "use-divide-function",
    "Use DIVIDE for division",
    "DAX",
    "warning",
    "DIVIDE returns blank for a zero or blank denominator instead of an error.",
    bpa="USE_THE_DIVIDE_FUNCTION_FOR_DIVISION",
)
def _divide(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if has_division_operator(m.expression):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] divides with `/`."
    for t, c in model.all_columns():
        if c.kind == "calculated" and c.expression and has_division_operator(c.expression):
            yield _cpath(model, t, c), f"Calculated column {t.name}[{c.name}] divides with `/`."


@rule(
    "avoid-iferror",
    "Avoid IFERROR",
    "DAX",
    "warning",
    "IFERROR forces row-by-row evaluation; DIVIDE or explicit checks are faster.",
    bpa="AVOID_USING_THE_IFERROR_FUNCTION",
)
def _iferror(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if calls(m.expression, "IFERROR"):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] uses IFERROR."
    for t, c in model.all_columns():
        if c.kind == "calculated" and c.expression and calls(c.expression, "IFERROR"):
            yield _cpath(model, t, c), f"Calculated column {t.name}[{c.name}] uses IFERROR."


@rule(
    "use-treatas",
    "Use TREATAS instead of INTERSECT",
    "DAX",
    "warning",
    "TREATAS is faster for virtual relationships.",
    bpa="USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT",
)
def _treatas(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if calls(m.expression, "INTERSECT"):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] uses INTERSECT."


@rule(
    "no-evaluateandlog",
    "Remove EVALUATEANDLOG",
    "DAX",
    "info",
    "EVALUATEANDLOG is for debugging, not production models.",
    bpa="EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS",
)
def _evaluateandlog(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if calls(m.expression, "EVALUATEANDLOG"):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] uses EVALUATEANDLOG."


@rule(
    "avoid-1-minus-x-over-y",
    "Avoid the 1-(x/y) syntax",
    "DAX",
    "warning",
    "1-(x/y) always returns a value, which can add rows; use DIVIDE(x - y, x) instead.",
    bpa="AVOID_USING_'1-(X/Y)'_SYNTAX",
)
def _one_minus(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    pattern = re.compile(
        r"[0-9]+\s*[-+]\s*[(]*\s*(SUM\s*\(\s*'*[A-Za-z0-9 _]+'*\s*\[[A-Za-z0-9 _]+\]"
        r"\s*\)\s*/|DIVIDE\s*\()",
        re.I,
    )
    for t, m in model.all_measures():
        if pattern.search(code_only(m.expression)):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] uses the 1-(x/y) pattern."


@rule(
    "measure-direct-reference",
    "Measures should not just reference another measure",
    "DAX",
    "warning",
    "A measure that only returns another measure duplicates it.",
    bpa="MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES",
)
def _direct_ref(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        refs = references(m.expression)
        stripped = re.sub(r"[\s()]", "", code_only(m.expression))
        if (
            len(refs) == 1
            and ix.is_measure(refs[0])
            and stripped.endswith("]")
            and stripped.count("[") == 1
            and re.fullmatch(r"('[^']*'|\w+)?\[[^\]]*\]", stripped)
        ):
            yield _mpath(model, t, m.name), f"Measure [{m.name}] only returns [{refs[0].name}]."


@rule(
    "avoid-duplicate-measures",
    "No two measures should have the same definition",
    "DAX",
    "warning",
    "Duplicate measures drift apart over time and confuse readers.",
    bpa="AVOID_DUPLICATE_MEASURES",
)
def _duplicates(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    groups: dict[str, list[tuple[Table, str]]] = defaultdict(list)
    for t, m in model.all_measures():
        if m.expression.strip():
            groups[normalized(m.expression)].append((t, m.name))
    for same in groups.values():
        if len(same) > 1:
            names = ", ".join(f"[{n}]" for _, n in same)
            for t, n in same:
                yield _mpath(model, t, n), f"Measures {names} have the same definition."


@rule(
    "dax-columns-fully-qualified",
    "Column references should be fully qualified",
    "DAX",
    "warning",
    "Table[Column] makes columns easy to tell apart from [Measures].",
    bpa="DAX_COLUMNS_FULLY_QUALIFIED",
)
def _cols_qualified(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        bare = sorted(
            {
                r.name
                for r in references(m.expression)
                if r.table is None and not ix.is_measure(r) and r.name.casefold() in ix.column_names
            }
        )
        if bare:
            yield (
                _mpath(model, t, m.name),
                f"Measure [{m.name}] references column(s) "
                f"{', '.join(f'[{b}]' for b in bare)} without the table name.",
            )


@rule(
    "dax-measures-unqualified",
    "Measure references should be unqualified",
    "DAX",
    "warning",
    "[Measure] without a table name makes measures easy to tell apart from columns.",
    bpa="DAX_MEASURES_UNQUALIFIED",
)
def _measures_unqualified(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        qualified = sorted(
            {
                f"{r.table}[{r.name}]"
                for r in references(m.expression)
                if r.table is not None and ix.is_measure(r)
            }
        )
        if qualified:
            yield (
                _mpath(model, t, m.name),
                f"Measure [{m.name}] names the table for measure(s) {', '.join(qualified)}.",
            )


@rule(
    "inactive-relationship-unused",
    "Inactive relationships that are never activated",
    "DAX",
    "warning",
    "An inactive relationship no measure activates with USERELATIONSHIP does nothing.",
    bpa="INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED",
)
def _inactive(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for r in model.relationships:
        pair = frozenset(
            (
                (r.from_table.casefold(), r.from_column.casefold()),
                (r.to_table.casefold(), r.to_column.casefold()),
            )
        )
        if not r.is_active and pair not in ix.activated:
            yield (
                make_path(model.name, "relationship", r.name),
                f"Inactive relationship {r.label} is never used by USERELATIONSHIP.",
            )


# ---------------------------------------------------------------- Error prevention


@rule(
    "data-column-needs-source",
    "Data columns must have a source column",
    "Error prevention",
    "error",
    "A data column without a source column fails on refresh.",
    bpa="DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN",
)
def _source_column(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if c.kind == "data" and not (c.source_column or "").strip():
            yield _cpath(model, t, c), f"Column {t.name}[{c.name}] has no source column."


@rule(
    "expression-required",
    "Measures and calculated columns need an expression",
    "Error prevention",
    "error",
    "Without an expression the object shows no values.",
    bpa="EXPRESSION_RELIANT_OBJECTS_MUST_HAVE_AN_EXPRESSION",
)
def _expression(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if not m.expression.strip():
            yield _mpath(model, t, m.name), f"Measure [{m.name}] has no expression."
    for t, c in model.all_columns():
        if c.kind == "calculated" and not (c.expression or "").strip():
            yield _cpath(model, t, c), f"Calculated column {t.name}[{c.name}] has no expression."


@rule(
    "relationship-same-data-type",
    "Relationship columns should have the same data type",
    "Error prevention",
    "error",
    "Mismatched key types break or slow down relationships.",
    bpa="RELATIONSHIP_COLUMNS_SAME_DATA_TYPE",
)
def _rel_types(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for r in model.relationships:
        ft, tt = model.tables.get(r.from_table), model.tables.get(r.to_table)
        fc = ft.column(r.from_column) if ft else None
        tc = tt.column(r.to_column) if tt else None
        if fc and tc and fc.data_type and tc.data_type and fc.data_type != tc.data_type:
            yield (
                make_path(model.name, "relationship", r.name),
                f"Relationship {r.label} joins {fc.data_type} to {tc.data_type}.",
            )


@rule(
    "invalid-name-characters",
    "Avoid invalid characters in names",
    "Error prevention",
    "error",
    "Control characters in names make deployment fail.",
    bpa="AVOID_INVALID_NAME_CHARACTERS",
)
def _invalid_chars(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    def bad(name: str) -> bool:
        return any(ord(ch) < 32 and not ch.isspace() for ch in name)

    for t in model.tables.values():
        if bad(t.name):
            yield make_path(model.name, t.name), f"Table name {t.name!r} has control characters."
        for c in t.columns:
            if bad(c.name):
                yield _cpath(model, t, c), f"Column name {c.name!r} has control characters."
        for m in t.measures:
            if bad(m.name):
                yield _mpath(model, t, m.name), f"Measure name {m.name!r} has control characters."


# ---------------------------------------------------------------- Maintenance


@rule(
    "unnecessary-hidden-columns",
    "Remove unnecessary hidden columns",
    "Maintenance",
    "warning",
    "Hidden columns nothing uses still cost memory and refresh time. (Report visuals "
    "and row-level security aren't checked yet, so confirm before removing.)",
    bpa="UNNECESSARY_COLUMNS",
)
def _unused_columns(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    related = {r.from_table for r in model.relationships} | {
        r.to_table for r in model.relationships
    }
    for t in model.tables.values():
        if t.is_auto_date or t.is_field_parameter:
            continue
        unused = []
        for c in t.columns:
            if not (c.is_hidden or t.is_hidden):
                continue
            key = (t.name.casefold(), c.name.casefold())
            if not (
                ix.is_column_referenced(t.name, c.name)
                or key in ix.relationship_columns
                or key in ix.sort_by_columns
                or key in ix.hierarchy_columns
            ):
                unused.append(c)
        whole_table = (
            t.is_hidden
            and t.columns
            and len(unused) == len(t.columns)
            and not t.measures
            and t.name not in related
        )
        if whole_table:
            yield (
                make_path(model.name, t.name),
                (
                    f"Hidden table {t.name} is not used by any measure or relationship. "
                    "If it only feeds another query, turn off Enable load."
                ),
            )
            continue
        for c in unused:
            yield (
                _cpath(model, t, c),
                (
                    f"Hidden column {t.name}[{c.name}] is not used by any measure, relationship, "
                    "sort order or hierarchy."
                ),
            )


@rule(
    "unnecessary-hidden-measures",
    "Remove unnecessary hidden measures",
    "Maintenance",
    "warning",
    "Hidden measures nothing references are dead code.",
    bpa="UNNECESSARY_MEASURES",
)
def _unused_measures(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if (m.is_hidden or t.is_hidden) and m.name.casefold() not in ix.referenced_measures:
            yield _mpath(model, t, m.name), f"Hidden measure [{m.name}] is not used anywhere."


@rule(
    "table-without-relationships",
    "Tables should have relationships",
    "Maintenance",
    "info",
    "A table with no relationships can't filter or be filtered by the rest of the model.",
    bpa="ENSURE_TABLES_HAVE_RELATIONSHIPS",
)
def _no_relationships(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    related = {r.from_table for r in model.relationships} | {
        r.to_table for r in model.relationships
    }
    for t in model.tables.values():
        measure_table = bool(t.measures) and len(t.columns) <= 1
        if t.name not in related and not t.is_auto_date and not measure_table:
            yield make_path(model.name, t.name), f"Table {t.name} has no relationships."


@rule(
    "measure-missing-description",
    "Measures should have descriptions",
    "AI readiness",
    "warning",
    "People and Copilot can't tell what an undocumented measure means.",
)
def _measure_desc(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if _visible(t, m.is_hidden) and not (m.description or "").strip():
            yield _mpath(model, t, m.name), f"Measure [{m.name}] has no description."


@rule(
    "object-missing-description",
    "Visible tables and columns should have descriptions",
    "AI readiness",
    "info",
    "Descriptions show in the field list and ground Copilot's answers.",
    bpa="OBJECTS_WITH_NO_DESCRIPTION",
)
def _object_desc(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t in model.tables.values():
        if t.is_hidden or t.is_auto_date:
            continue
        if not (t.description or "").strip():
            yield make_path(model.name, t.name), f"Table {t.name} has no description."
        for c in t.columns:
            if not c.is_hidden and not (c.description or "").strip():
                yield _cpath(model, t, c), f"Column {t.name}[{c.name}] has no description."


# ---------------------------------------------------------------- Naming and formatting


@rule(
    "name-leading-trailing-space",
    "Names should not start or end with a space",
    "Naming",
    "error",
    "Leading or trailing spaces break references and confuse users.",
    bpa="OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE",
)
def _spaces(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    def bad(name: str) -> bool:
        return name != name.strip(" ")

    for t in model.tables.values():
        if bad(t.name):
            yield make_path(model.name, t.name), f"Table name {t.name!r} has extra spaces."
        for c in t.columns:
            if bad(c.name):
                yield _cpath(model, t, c), f"Column name {c.name!r} has extra spaces."
        for m in t.measures:
            if bad(m.name):
                yield _mpath(model, t, m.name), f"Measure name {m.name!r} has extra spaces."


@rule(
    "measure-format-string",
    "Visible measures need a format string",
    "Formatting",
    "warning",
    "Without a format string, numbers show with inconsistent decimals.",
    bpa="PROVIDE_FORMAT_STRING_FOR_MEASURES",
)
def _measure_format(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, m in model.all_measures():
        if _visible(t, m.is_hidden) and not (m.format_string or "").strip():
            yield _mpath(model, t, m.name), f"Measure [{m.name}] has no format string."


@rule(
    "numeric-column-summarize-by",
    "Do not summarize numeric columns",
    "Formatting",
    "warning",
    "Implicit sums of keys or prices give wrong totals; use explicit measures.",
    bpa="NUMERIC_COLUMN_SUMMARIZE_BY",
)
def _summarize(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if c.data_type in NUMERIC and _visible(t, c.is_hidden) and c.summarize_by != "none":
            how = c.summarize_by or "default"
            yield _cpath(model, t, c), f"Numeric column {t.name}[{c.name}] summarizes by {how}."


@rule(
    "hide-foreign-keys",
    "Hide foreign keys",
    "Formatting",
    "warning",
    "Foreign keys on the many side confuse report builders; filter through the dimension.",
    bpa="HIDE_FOREIGN_KEYS",
)
def _hide_fk(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    seen = set()
    for r in model.relationships:
        if r.from_cardinality != "many":
            continue
        t = model.tables.get(r.from_table)
        c = t.column(r.from_column) if t else None
        if t and c and not c.is_hidden and not t.is_hidden and (t.name, c.name) not in seen:
            seen.add((t.name, c.name))
            yield _cpath(model, t, c), f"Foreign key {t.name}[{c.name}] is visible."


@rule(
    "mark-primary-keys",
    "Mark primary keys",
    "Formatting",
    "info",
    "Marking the key column helps the engine and tools understand the table.",
    bpa="MARK_PRIMARY_KEYS",
)
def _mark_pk(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    seen = set()
    for r in model.relationships:
        if r.to_cardinality != "one":
            continue
        t = model.tables.get(r.to_table)
        c = t.column(r.to_column) if t else None
        if t and c and not c.is_key and t.data_category != "Time" and (t.name, c.name) not in seen:
            seen.add((t.name, c.name))
            yield _cpath(model, t, c), f"{t.name}[{c.name}] is a primary key but not marked as key."


@rule(
    "sort-month-columns",
    "Month names must be sorted by month number",
    "Formatting",
    "warning",
    "Unsorted month names sort alphabetically (April, August, ...).",
    bpa="MONTH_(AS_A_STRING)_MUST_BE_SORTED",
)
def _month_sort(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        name = c.name.upper()
        if (
            "MONTH" in name
            and "MONTHS" not in name
            and c.data_type == "string"
            and not c.sort_by_column
            and not t.is_auto_date
        ):
            yield _cpath(model, t, c), f"{t.name}[{c.name}] is a month name with no sort column."


@rule(
    "add-data-category",
    "Add a data category to geography columns",
    "Formatting",
    "info",
    "Map visuals and Copilot need to know a column holds cities, countries or coordinates.",
    bpa="ADD_DATA_CATEGORY_FOR_COLUMNS",
)
def _data_category(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if c.data_category:
            continue
        lower = c.name.lower()
        geo_text = (
            any(w in lower for w in ("country", "continent", "city")) and c.data_type == "string"
        )
        coord = lower in ("latitude", "longitude") and c.data_type in ("decimal", "double")
        if geo_text or coord:
            yield _cpath(model, t, c), f"{t.name}[{c.name}] has no data category."


# ---------------------------------------------------------------- Power Query (refresh risks)

_LOCAL_PATH = re.compile(r'"(?:[A-Za-z]:\\|\\\\)[^"]*"')
_SOURCES = re.compile(
    r"\b(Csv\.Document|Excel\.Workbook|Json\.Document|Web\.Contents|"
    r"Folder\.Files|SharePoint\.Files|Xml\.Tables)\s*\("
)


def _m_sources(model: SemanticModel) -> Iterator[tuple[str, str, str]]:
    """(object_path, label, M code) for every M partition and shared non-parameter query."""
    for t in model.tables.values():
        for p in t.partitions:
            if p.kind == "m" and p.source:
                yield (
                    make_path(model.name, t.name, "partition", p.name),
                    f"table {t.name}",
                    p.source,
                )
    for name, expr in model.expressions.items():
        if "IsParameterQuery=true" not in expr.replace(" ", ""):
            yield make_path(model.name, "expression", name), f"query {name}", expr


@rule(
    "pq-hardcoded-local-path",
    "Don't hard-code local file paths in Power Query",
    "Power Query",
    "warning",
    "A path like C:\\... only exists on one PC: refresh fails in the Service without a gateway "
    "and breaks for colleagues. Use a parameter.",
)
def _local_paths(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for path, label, code in _m_sources(model):
        found = _LOCAL_PATH.search(code)
        if found:
            yield path, f"Power Query for {label} reads the local path {found.group(0)}."


@rule(
    "pq-untyped-source",
    "Set column types after loading files or web data",
    "Power Query",
    "info",
    "Without a Changed Type step, column types are guessed and can break refresh.",
)
def _untyped(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for path, label, code in _m_sources(model):
        if _SOURCES.search(code) and "Table.TransformColumnTypes" not in code:
            yield path, f"Power Query for {label} loads a file or web source without setting types."


# ---------------------------------------------------------------- AI readiness

_GENERIC = re.compile(r"(Column\d+|Custom(\.\d+)?|Attribute|Value|Field\d*)", re.I)


@rule(
    "generic-column-name",
    "Rename generic columns",
    "AI readiness",
    "info",
    "Names like Column1 or Value mean nothing to users or Copilot.",
)
def _generic(model: SemanticModel, ix: ModelIndex) -> Iterable[tuple[str, str]]:
    for t, c in model.all_columns():
        if _visible(t, c.is_hidden) and _GENERIC.fullmatch(c.name):
            yield _cpath(model, t, c), f"{t.name}[{c.name}] has a generic name."


# ---------------------------------------------------------------- running


def run_rules(models: Iterable[SemanticModel]) -> list[RuleFinding]:
    out: list[RuleFinding] = []
    for model in models:
        ix = ModelIndex.build(model)
        for info, check in RULES:
            for path, message in check(model, ix):
                out.append(RuleFinding(info.id, info.severity, path, message, info.category))
    return out


def rule_info(rule_id: str) -> RuleInfo | None:
    return next((info for info, _ in RULES if info.id == rule_id), None)
