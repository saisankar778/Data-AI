"""Refresh doctor: recognise a Power BI refresh error and point at the model objects involved.

Deterministic: a library of known error patterns, plus locators that search the model's Power
Query code, columns and relationships for the names the error mentions. The output is evidence
a person (or Claude) can act on, not a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from datawright.connectors.powerbi.model import SemanticModel
from datawright.core.paths import make_path


@dataclass
class Evidence:
    object_path: str
    note: str
    code: str | None = None  # the Power Query step or DAX involved


@dataclass
class Diagnosis:
    pattern: str
    title: str
    cause: str
    fix: str
    matched: str
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class _Pattern:
    id: str
    regex: re.Pattern[str]
    title: str
    cause: str
    fix: str
    locate: Callable[[SemanticModel, re.Match[str]], list[Evidence]] | None = None


# ---------------------------------------------------------------- Power Query helpers

_STEP = re.compile(r'^\s*(#"(?:[^"]|"")+"|[A-Za-z_][\w.]*)\s*=', re.M)


def m_steps(m: str) -> list[tuple[str, str]]:
    """(step name, step code) for a `let ... in` query; approximate but robust for real queries."""
    body = m
    let = re.search(r"\blet\b", m)
    if let:
        body = m[let.end() :]
        last_in = list(re.finditer(r"^\s*in\b", body, re.M))
        if last_in:
            body = body[: last_in[-1].start()]
    starts = list(_STEP.finditer(body))
    steps = []
    for i, s in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        name = s.group(1)
        if name.startswith('#"'):
            name = name[2:-1].replace('""', '"')
        steps.append((name, body[s.end() : end].strip().rstrip(",")))
    return steps


def _queries(model: SemanticModel) -> list[tuple[str, str, str]]:
    """(object_path, label, M code) for every M partition and shared query."""
    out = []
    for t in model.tables.values():
        for p in t.partitions:
            if p.kind == "m" and p.source:
                out.append((make_path(model.name, t.name, "partition", p.name),
                            f"table {t.name}", p.source))  # fmt: skip
    for name, expr in model.expressions.items():
        out.append((make_path(model.name, "expression", name), f"query {name}", expr))
    return out


def _steps_mentioning(model: SemanticModel, needle: str, quoted: bool = True) -> list[Evidence]:
    target = f'"{needle}"' if quoted else needle
    found = []
    for path, label, code in _queries(model):
        for step, body in m_steps(code) or [("(query)", code)]:
            if target.casefold() in body.casefold():
                found.append(Evidence(path, f"{label}, step \"{step}\" uses {needle}",
                                      _clip(body)))  # fmt: skip
    return found


def _clip(code: str, limit: int = 240) -> str:
    code = " ".join(code.split())
    return code if len(code) <= limit else code[: limit - 1] + "…"


# ---------------------------------------------------------------- locators


def _locate_path(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    path = m.group("path")
    name = re.split(r"[\\/]", path.rstrip("\\/"))[-1]
    found = _steps_mentioning(model, path, quoted=False)
    if not found and name:
        found = _steps_mentioning(model, name, quoted=False)
    return found or _all_local_paths(model)


def _all_local_paths(model: SemanticModel) -> list[Evidence]:
    found = []
    for path, label, code in _queries(model):
        if "IsParameterQuery=true" in code.replace(" ", ""):
            continue
        for lit in re.findall(r'"(?:[A-Za-z]:\\|\\\\)[^"]*"', code):
            found.append(Evidence(path, f"{label} reads the local path {lit}"))
    return found


def _locate_column(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    col = m.group("col")
    found = _steps_mentioning(model, col)
    for t in model.tables.values():
        for c in t.columns:
            if (c.source_column or "").casefold() == col.casefold() or c.name.casefold() == \
                    col.casefold():  # fmt: skip
                found.append(
                    Evidence(
                        make_path(model.name, t.name, "column", c.name),
                        f"model column {t.name}[{c.name}] is loaded from it",
                    )
                )
    return found


def _locate_type(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    found = []
    wanted = (m.groupdict().get("type") or "").lower()
    for path, label, code in _queries(model):
        for step, body in m_steps(code):
            if "Table.TransformColumnTypes" in body or "Table.TransformColumns" in body:
                if not wanted or wanted[:4] in body.lower() or wanted in ("number", "date"):
                    found.append(Evidence(path, f"{label}, step \"{step}\" sets column types",
                                          _clip(body)))  # fmt: skip
    return found


def _locate_key(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    col, table = m.group("col"), m.groupdict().get("table")
    found = []
    for r in model.relationships:
        one_side = (r.to_table, r.to_column) if r.to_cardinality == "one" else None
        if one_side and one_side[1].casefold() == col.casefold() and (
                table is None or one_side[0].casefold() == table.casefold()):  # fmt: skip
            found.append(Evidence(make_path(model.name, "relationship", r.name),
                                  f"relationship {r.label} needs {one_side[0]}[{one_side[1]}] "
                                  "to be unique and not blank"))  # fmt: skip
    if table and table in model.tables:
        for p in model.tables[table].partitions:
            found.append(Evidence(make_path(model.name, table, "partition", p.name),
                                  f"remove duplicates/blanks on {col} in this query",
                                  _clip(p.source or "")))  # fmt: skip
    return found


def _locate_excel_item(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    found = []
    for path, label, code in _queries(model):
        for step, body in m_steps(code):
            items = re.findall(r'Item\s*=\s*"([^"]+)"', body)
            if items:
                found.append(Evidence(path, f"{label}, step \"{step}\" opens sheet/table "
                                      f"{', '.join(items)}", _clip(body)))  # fmt: skip
    return found


def _locate_url(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    url = m.group("url")
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return _steps_mentioning(model, host, quoted=False)


def _locate_name(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    name = m.group("name")
    found = _steps_mentioning(model, name, quoted=False)
    if name not in model.expressions and model.expressions:
        found.append(Evidence(make_path(model.name), f"shared queries/parameters in the model: "
                              f"{', '.join(sorted(model.expressions))}"))  # fmt: skip
    return found


def _locate_unused(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    from datawright.connectors.powerbi.rules import run_rules

    found = [
        Evidence(f.object_path, f.message)
        for f in run_rules([model])
        if f.rule in ("unnecessary-hidden-columns", "auto-date-tables", "auto-date-time-on")
    ]
    return found[:15]


def _locate_sources(model: SemanticModel, m: re.Match[str]) -> list[Evidence]:
    found = _all_local_paths(model)
    for path, label, code in _queries(model):
        for fn in sorted(
            set(
                re.findall(
                    r"\b(Sql\.Database|Oracle\.Database|Odbc\.\w+|OleDb\.\w+|"
                    r"Folder\.Files|File\.Contents)\s*\(",
                    code,
                )
            )
        ):
            found.append(Evidence(path, f"{label} uses {fn} (on-premises source)"))
    return found


_I = re.I | re.S
PATTERNS: list[_Pattern] = [
    _Pattern(
        "file-not-found",
        re.compile(
            r"(?:Could not find (?:file|a part of the path)|couldn't find the file)\s*"
            r"'(?P<path>[^']+)'",
            _I,
        ),
        "A file the query reads isn't there",
        "The file was moved, renamed or deleted, or the refresh runs on a machine (or in the "
        "Power BI Service) where this local path doesn't exist.",
        "Put the folder in a Power Query parameter and point it at the new location; for "
        "scheduled refresh, keep the file on SharePoint/OneDrive or set up a gateway that can "
        "see the path.",
        _locate_path,
    ),
    _Pattern(
        "column-not-found",
        re.compile(r"The column '(?P<col>[^']+)' of the table wasn't found", _I),
        "A source column was renamed or removed",
        "A step refers to a column by name, and the source no longer has a column with that "
        "name (usually renamed or dropped upstream).",
        "Update the step that names the column (often Changed Type or Renamed Columns) to the "
        "new name, or remove it from that step. Check the model columns loaded from it.",
        _locate_column,
    ),
    _Pattern(
        "field-not-found",
        re.compile(r"The field '(?P<col>[^']+)' of the record wasn't found", _I),
        "A field was renamed or removed in the source",
        "A step expands or reads a record field that the source no longer returns.",
        "Update the step to the new field name, or use MissingField.Ignore where a missing "
        "field is acceptable.",
        _locate_column,
    ),
    _Pattern(
        "type-conversion",
        re.compile(
            r"(?:DataFormat\.Error: )?We couldn't convert(?: the value)?[^.\n]*?to "
            r"(?:type )?(?P<type>Number|Date|DateTime|Logical|Time|Duration)",
            _I,
        ),
        "A value doesn't match the column type",
        "A column is typed as a number or date, but some source values aren't (text, blanks "
        "written as '-', or a different date/number locale).",
        "In the Changed Type step: fix the type, use 'Using locale' for dates/decimals, or "
        "add Replace Errors / a cleaning step before it. Find the bad rows with Keep Errors.",
        _locate_type,
    ),
    _Pattern(
        "duplicate-key",
        re.compile(
            r"Column '(?P<col>[^']+)' in Table '(?P<table>[^']+)' contains a duplicate "
            r"value '(?P<value>[^']*)'",
            _I,
        ),
        "Duplicate key on the 'one' side of a relationship",
        "A dimension table now has the same key value twice, so the many-to-one relationship "
        "can't be built.",
        "Remove duplicates on the key column in that table's query (or fix the source); if "
        "duplicates are valid, change the relationship's cardinality deliberately.",
        _locate_key,
    ),
    _Pattern(
        "blank-key",
        re.compile(
            r"Column '(?P<col>[^']+)' in Table '(?P<table>[^']+)' contains blank "
            r"values",
            _I,
        ),
        "Blank key on the 'one' side of a relationship",
        "The key column of a dimension has blank values, which the relationship doesn't allow.",
        "Filter out or replace blank keys in that table's query.",
        _locate_key,
    ),
    _Pattern(
        "formula-firewall",
        re.compile(r"Formula\.Firewall", _I),
        "Privacy levels block combining these sources",
        "A query combines data from sources with different privacy levels (or reads one "
        "source inside another's steps), so Power Query refuses to run it.",
        "Set matching privacy levels (File > Options > Privacy, and in the Service's data "
        "source settings), or split the logic so each source is loaded in its own query "
        "first.",
        None,
    ),
    _Pattern(
        "credentials",
        re.compile(
            r"(?:credentials (?:provided|are)[^.\n]*invalid|Invalid credentials|"
            r"Login failed for user|AADSTS\d+|access token has expired|"
            r"\(401\)|\(403\))",
            _I,
        ),
        "Credentials are missing, expired or wrong",
        "The data source rejected the sign-in (password changed, token expired, or the "
        "account lost access).",
        "Desktop: Data source settings > Edit permissions. Service: semantic model settings > "
        "Data source credentials > Edit credentials, then refresh.",
        None,
    ),
    _Pattern(
        "gateway",
        re.compile(
            r"(?:gateway[^.\n]*(?:unreachable|offline|not reachable|isn't (?:available|"
            r"reachable)|not configured)|data sources currently don't support refresh|"
            r"you need to install a gateway)",
            _I,
        ),
        "The Service can't reach an on-premises source",
        "Scheduled refresh in the Power BI Service needs a gateway for local files, network "
        "folders and on-premises databases.",
        "Install/start an on-premises data gateway and map each source to it, or move files to "
        "SharePoint/OneDrive.",
        _locate_sources,
    ),
    _Pattern(
        "network",
        re.compile(
            r"(?:A network-related or instance-specific error|server was not found|"
            r"No connection could be made|could not be resolved)",
            _I,
        ),
        "The server can't be reached",
        "The database server name is wrong, the server is down, or a firewall/VPN blocks it.",
        "Check the server name in the Source step, connectivity from the refreshing machine "
        "(or gateway), and firewall rules.",
        _locate_sources,
    ),
    _Pattern(
        "excel-item",
        re.compile(r"The key didn't match any rows in the table", _I),
        "A sheet, table or named range is missing",
        "The workbook's sheet/table name changed (or a navigation step picks an item that no "
        "longer exists).",
        "Fix the Navigation step's Item name to the current sheet/table name.",
        _locate_excel_item,
    ),
    _Pattern(
        "web-error",
        re.compile(
            r"Web\.Contents failed to get contents from '(?P<url>[^']+)' "
            r"\((?P<status>\d{3})\)",
            _I,
        ),
        "A web/API call failed",
        "The web source returned an HTTP error (401/403 access, 404 moved, 429 throttled, "
        "5xx server problem).",
        "Check the URL and credentials; for 429 add paging/throttling; for 404 update the URL.",
        _locate_url,
    ),
    _Pattern(
        "missing-query",
        re.compile(
            r"The (?:import|name) '?(?P<name>[\w .#\"]+?)'? (?:matches no exports|"
            r"wasn't recognized)",
            _I,
        ),
        "A query, parameter or function name doesn't exist",
        "A step refers to a query/parameter/function that was renamed or deleted (or is "
        "misspelled).",
        "Rename the reference to the current query name, or restore the missing query.",
        _locate_name,
    ),
    _Pattern(
        "memory",
        re.compile(
            r"(?:not enough memory|Resource Governing|exceeded the (?:maximum )?memory|"
            r"memory (?:limit|error)|out of memory)",
            _I,
        ),
        "The refresh ran out of memory",
        "The model (or one refresh) needs more memory than the capacity or machine allows.",
        "Remove columns and tables nothing uses, turn off auto date/time, reduce high-"
        "cardinality columns, and use incremental refresh for big tables.",
        _locate_unused,
    ),
    _Pattern(
        "timeout",
        re.compile(
            r"(?:timed out|timeout expired|exceeded the (?:maximum )?(?:time|duration)|"
            r"refresh .* (?:2|5) hours)",
            _I,
        ),
        "The refresh took too long",
        "A query is slow (often because Power Query can't fold work back to the source) or "
        "the data is too big for one refresh.",
        "Filter and remove columns early so steps fold to the source, check View Native "
        "Query, and use incremental refresh.",
        _locate_sources,
    ),
]


def diagnose(error_text: str, models: list[SemanticModel]) -> list[Diagnosis]:
    """Every known pattern the error matches, with evidence from each model."""
    out = []
    for p in PATTERNS:
        m = p.regex.search(error_text)
        if not m:
            continue
        evidence: list[Evidence] = []
        if p.locate is not None:
            for model in models:
                evidence += p.locate(model, m)
        out.append(Diagnosis(p.id, p.title, p.cause, p.fix, _clip(m.group(0), 200),
                             _dedupe(evidence)))  # fmt: skip
    return out


def _dedupe(items: list[Evidence]) -> list[Evidence]:
    seen, out = set(), []
    for e in items:
        key = (e.object_path, e.note)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out
