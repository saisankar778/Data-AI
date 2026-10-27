"""Automatic fixes for audit findings, and how-to guidance for the ones that need a person.

A fix is a list of TMDL edit intents (the same ops change sets already validate and replay).
Only mechanical, low-risk changes are automatic: a property flip, a format string, a sort column,
a data category, or rewriting a plain `A / B` as DIVIDE(A, B). Anything that deletes, renames or
needs judgement stays manual, with guidance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from datawright.connectors.powerbi.dax import calls, has_division_operator, simple_division
from datawright.connectors.powerbi.model import Column, Table
from datawright.connectors.powerbi.pbip import ObjectRef, PowerBIConnector
from datawright.connectors.powerbi.rules import RuleFinding
from datawright.connectors.powerbi.tmdl import quote_name

GUIDANCE: dict[str, str] = {
    "avoid-floating-point": "In Power Query, change the column type to Fixed decimal number "
    "(Currency.Type) or Whole number, then refresh.",
    "mark-date-table": "Table tools > Mark as date table, and pick the date column.",
    "auto-date-tables": "Turn off auto date/time (File > Options > Current file > Data load); "
    "Desktop then removes these tables.",
    "auto-date-time-on": "File > Options > Current file > Data load > untick Auto date/time.",
    "model-has-date-table": "Add a date table (for example CALENDARAUTO() or one from Power "
    "Query), relate it to your date columns and mark it as a date table.",
    "many-to-many-single-direction": "Set Cross filter direction to Single, or model the bridge "
    "table explicitly.",
    "avoid-bidirectional-relationship": "Set the relationship's cross filter direction to Single; "
    "where you really need two-way filtering, use CROSSFILTER() inside the measures that need it.",
    "calculated-column-uses-related": "Merge the looked-up column in Power Query instead of a "
    "RELATED() calculated column.",
    "use-divide-function": "Rewrite `a / b` as DIVIDE(a, b). Only plain `A / B` measures are fixed "
    "automatically; others need a person or `datawright document`-style help from Claude.",
    "avoid-iferror": "Replace IFERROR(x / y, ...) with DIVIDE(x, y); otherwise test the error "
    "condition explicitly.",
    "use-treatas": "Replace INTERSECT() in the virtual relationship with TREATAS().",
    "no-evaluateandlog": "Remove EVALUATEANDLOG() before publishing.",
    "avoid-1-minus-x-over-y": "Rewrite 1 - x / y as DIVIDE(y - x, y).",
    "measure-direct-reference": "Delete the duplicate measure and point visuals at the original "
    "(or give it its own logic).",
    "avoid-duplicate-measures": "Keep one of the measures and point visuals at it; delete the "
    "other.",
    "dax-columns-fully-qualified": "Write column references as Table[Column].",
    "dax-measures-unqualified": "Write measure references as [Measure], without the table name.",
    "inactive-relationship-unused": "Delete the relationship, or use it in a measure with "
    "USERELATIONSHIP().",
    "data-column-needs-source": "Set the column's source column, or remove the column.",
    "expression-required": "Give the object an expression, or delete it.",
    "relationship-same-data-type": "Change one key column's type in Power Query so both sides "
    "match (ideally whole numbers).",
    "invalid-name-characters": "Rename the object without control characters.",
    "unnecessary-hidden-columns": "Remove the column in Power Query (Choose columns), after "
    "checking no report visual or RLS role uses it.",
    "unnecessary-hidden-measures": "Delete the measure after checking no report uses it.",
    "table-without-relationships": "Relate the table to the model, or delete it if it isn't used.",
    "measure-missing-description": "Run `datawright document` (Claude drafts them for review).",
    "object-missing-description": "Run `datawright document` (Claude drafts them for review).",
    "name-leading-trailing-space": "Rename the object without the extra space, then fix any "
    "visuals that used the old name.",
    "measure-format-string": "Set a format string in Measure tools > Format.",
    "numeric-column-summarize-by": "Column tools > Summarization > Don't summarize; use "
    "explicit measures for totals.",
    "hide-foreign-keys": "Hide the column (right-click > Hide in report view).",
    "mark-primary-keys": "Set the column's Key property (Model view > Properties > Advanced).",
    "sort-month-columns": "Column tools > Sort by column > the month number column.",
    "add-data-category": "Column tools > Data category > City / Country or Region / Latitude ...",
    "pq-hardcoded-local-path": "Create a text parameter for the folder (Manage parameters) and "
    "use it in the Source step, or move the file to SharePoint/OneDrive.",
    "pq-untyped-source": "Add a Changed Type step (Transform > Detect data type) after loading.",
    "generic-column-name": "Rename the column to say what it holds.",
}

AUTO_FIXABLE = frozenset({
    "numeric-column-summarize-by", "hide-foreign-keys", "mark-primary-keys",
    "measure-format-string", "sort-month-columns", "add-data-category", "mark-date-table",
    "use-divide-function",
})  # fmt: skip

_PERCENT = re.compile(r"%|\b(rate|ratio|margin|pct|percent|share|growth|yoy|mom)\b", re.I)
_MONTH_NUMBER = re.compile(r"month\s*_?\s*(num(ber)?|no|index|key|sort|order|id)?$", re.I)


@dataclass
class Fix:
    rule: str
    object_path: str
    file_path: str
    intents: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


def guidance(rule: str) -> str:
    return GUIDANCE.get(rule, "")


def _prop(ref: ObjectRef, rule: str, key: str, value: str | bool, label: str) -> dict[str, Any]:
    return {
        "op": "tmdl.set_property", "kind": ref.kind,
        "table": None if ref.kind == "table" else ref.table.name, "name": ref.name,
        "key": key, "value": value, "object_path": ref.object_path, "rule": rule, "label": label,
    }  # fmt: skip


def format_string_for(name: str, expression: str) -> str:
    if _PERCENT.search(name):
        return "0.0%"
    ratio = calls(expression, "DIVIDE") or has_division_operator(expression)
    average = re.search(r"\b(avg|average|mean|per)\b", name, re.I) or calls(expression, "AVERAGE")
    counts = any(calls(expression, f) for f in ("COUNT", "COUNTA", "COUNTROWS", "DISTINCTCOUNT"))
    if counts and not ratio and not average:
        return "#,0"
    return "#,0.00"


def month_sort_column(table: Table, month: Column) -> Column | None:
    candidates = [
        c for c in table.columns
        if c is not month and c.data_type == "int64" and _MONTH_NUMBER.fullmatch(c.name.strip())
        and c.name.casefold() != month.name.casefold()
    ]  # fmt: skip
    return candidates[0] if len(candidates) == 1 else None


def data_category_for(column: Column) -> str | None:
    lower = column.name.lower()
    for word, category in (("country", "Country"), ("continent", "Continent"), ("city", "City")):
        if word in lower and column.data_type == "string":
            return category
    if lower in ("latitude", "longitude") and column.data_type in ("decimal", "double"):
        return lower.capitalize()
    return None


def plan_fix(connector: PowerBIConnector, finding: RuleFinding) -> Fix | None:
    """The automatic fix for one finding, or None if it needs a person."""
    if finding.rule not in AUTO_FIXABLE:
        return None
    try:
        ref = connector.resolve(finding.object_path)
    except KeyError:
        return None
    if ref.file_path is None:  # .pbix: read only
        return None
    fix = Fix(finding.rule, finding.object_path, ref.file_path)
    c, m = ref.column, ref.measure
    if finding.rule == "numeric-column-summarize-by" and c:
        fix.intents.append(_prop(ref, fix.rule, "summarizeBy", "none", "summarizeBy: none"))
        fix.summary = f"Don't summarize {ref.table.name}[{c.name}]"
    elif finding.rule == "hide-foreign-keys" and c:
        fix.intents.append(_prop(ref, fix.rule, "isHidden", True, "isHidden"))
        fix.summary = f"Hide foreign key {ref.table.name}[{c.name}]"
    elif finding.rule == "mark-primary-keys" and c:
        fix.intents.append(_prop(ref, fix.rule, "isKey", True, "isKey"))
        fix.summary = f"Mark {ref.table.name}[{c.name}] as the key"
    elif finding.rule == "measure-format-string" and m:
        fmt = format_string_for(m.name, m.expression)
        fix.intents.append(_prop(ref, fix.rule, "formatString", fmt, f"formatString: {fmt}"))
        fix.summary = f"Format [{m.name}] as {fmt}"
    elif finding.rule == "sort-month-columns" and c:
        by = month_sort_column(ref.table, c)
        if by is None:
            return None
        value = quote_name(by.name)
        fix.intents.append(_prop(ref, fix.rule, "sortByColumn", value, f"sortByColumn: {value}"))
        fix.summary = f"Sort {ref.table.name}[{c.name}] by [{by.name}]"
    elif finding.rule == "add-data-category" and c:
        category = data_category_for(c)
        if category is None:
            return None
        fix.intents.append(_prop(ref, fix.rule, "dataCategory", category,
                                 f"dataCategory: {category}"))  # fmt: skip
        fix.summary = f"Categorise {ref.table.name}[{c.name}] as {category}"
    elif finding.rule == "mark-date-table" and ref.kind == "table":
        t = ref.table
        dates = [x for x in t.columns if x.data_type == "dateTime"]
        key = next((x for x in dates if x.is_key), None)
        key = key or next((x for x in dates if x.name.casefold() == "date"), None)
        key = key or (dates[0] if len(dates) == 1 else None)
        if key is None:
            return None
        if t.data_category != "Time":
            fix.intents.append(_prop(ref, fix.rule, "dataCategory", "Time", "dataCategory: Time"))
        if not key.is_key:
            col_ref = connector.resolve(f"{ref.object_path}/column/{key.name}")
            fix.intents.append(_prop(col_ref, fix.rule, "isKey", True, "isKey"))
        fix.summary = f"Mark {t.name} as the date table (key {t.name}[{key.name}])"
    elif finding.rule == "use-divide-function" and m:
        parts = simple_division(m.expression)
        if parts is None or "\n" in m.expression:
            return None
        new = f"DIVIDE({parts[0]}, {parts[1]})"
        fix.intents.append({
            "op": "tmdl.set_expression", "kind": "measure", "table": ref.table.name,
            "name": m.name, "value": new, "object_path": ref.object_path, "rule": fix.rule,
            "label": f"= {new}",
        })  # fmt: skip
        fix.summary = f"Use DIVIDE in [{m.name}]"
    return fix if fix.intents else None


def plan_fixes(
    connector: PowerBIConnector, findings: list[RuleFinding], rules: set[str] | None = None
) -> tuple[list[Fix], list[RuleFinding]]:
    """(automatic fixes, findings that need a person)."""
    fixes, manual = [], []
    for f in findings:
        if rules and f.rule not in rules:
            continue
        fix = plan_fix(connector, f)
        if fix is None:
            manual.append(f)
        else:
            fixes.append(fix)
    # Marking a date table also resolves "model has no date table" for that model.
    marked = {f.object_path.split("/")[0] for f in fixes if f.rule == "mark-date-table"}
    manual = [
        m for m in manual if not (m.rule == "model-has-date-table" and m.object_path in marked)
    ]
    return fixes, manual
