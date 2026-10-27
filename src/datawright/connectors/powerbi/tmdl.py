"""Minimal TMDL reader and targeted editor.

The reader understands enough of TMDL to list tables, columns, measures and relationships with
their descriptions, expressions and properties. The editor changes only the lines it targets
(today: the `///` description block above an object) and keeps every other byte, the file's
newline style and any BOM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from datawright.connectors.powerbi.model import (
    Column,
    Hierarchy,
    Measure,
    Partition,
    Relationship,
    SemanticModel,
    Table,
)

OBJECT_KEYWORDS = frozenset(
    {
        "alternateOf",
        "annotation",
        "calculationGroup",
        "calculationItem",
        "changedProperty",
        "column",
        "culture",
        "dataSource",
        "database",
        "detailRowsDefinition",
        "expression",
        "extendedProperty",
        "formatStringDefinition",
        "hierarchy",
        "level",
        "linguisticMetadata",
        "measure",
        "model",
        "partition",
        "perspective",
        "queryGroup",
        "ref",
        "refreshPolicy",
        "relationship",
        "role",
        "table",
        "tablePermission",
        "variation",
    }
)
FENCE = "```"


class TmdlError(ValueError):
    pass


@dataclass
class TmdlObject:
    kind: str
    name: str
    line: int  # 0-based index of the declaration line
    indent: int
    description: str | None = None
    expression: str | None = None
    properties: dict[str, str] = field(default_factory=dict)
    children: list[TmdlObject] = field(default_factory=list)

    def child(self, kind: str, name: str) -> TmdlObject | None:
        return next((c for c in self.children if c.kind == kind and c.name == name), None)

    def structure(self, skip_description_of: tuple[str, str] | None = None) -> Any:
        """Comparable form of the object tree (used to prove an edit touched nothing else)."""
        desc = self.description
        if skip_description_of == (self.kind, self.name):
            desc = None
        return (
            self.kind,
            self.name,
            desc,
            self.expression,
            tuple(sorted(self.properties.items())),
            tuple(c.structure(skip_description_of) for c in self.children),
        )


@dataclass
class TmdlDocument:
    text: str
    objects: list[TmdlObject]

    def find(
        self, kind: str, name: str, parent: tuple[str, str] | None = None
    ) -> TmdlObject | None:
        scope = self.objects
        if parent is not None:
            owner = next((o for o in self.objects if (o.kind, o.name) == parent), None)
            if owner is None:
                return None
            scope = owner.children
        return next((o for o in scope if o.kind == kind and o.name == name), None)


def _indent_of(line: str) -> int:
    tabs = len(line) - len(line.lstrip("\t"))
    if tabs:
        return tabs
    spaces = len(line) - len(line.lstrip(" "))
    return spaces // 4


def _parse_name(rest: str) -> tuple[str, str]:
    """Return (name, remainder) where remainder starts after the name."""
    rest = rest.strip()
    if rest.startswith("'"):
        i, out = 1, []
        while i < len(rest):
            ch = rest[i]
            if ch == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                return "".join(out), rest[i + 1 :]
            out.append(ch)
            i += 1
        raise TmdlError(f"unterminated quoted name: {rest!r}")
    m = re.match(r"([^\s=:]+)(.*)$", rest)
    if not m:
        return "", rest
    return m.group(1), m.group(2)


def quote_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _dedent(lines: list[str]) -> str:
    non_blank = [ln for ln in lines if ln.strip()]
    if not non_blank:
        return ""
    lead = min(len(ln) - len(ln.lstrip("\t ")) for ln in non_blank)
    return "\n".join(ln[lead:] if ln.strip() else "" for ln in lines).strip("\n")


def parse(text: str) -> TmdlDocument:
    body = text[1:] if text.startswith("\ufeff") else text
    lines = body.splitlines()
    objects: list[TmdlObject] = []
    stack: list[TmdlObject] = []
    pending_desc: list[str] = []
    i = 0
    n = len(lines)

    def collect_expression(start: int, first: str, min_indent: int) -> tuple[str, int]:
        """Collect an expression whose first line remainder is `first`; return (expr, next_i)."""
        first = first.strip()
        if first == FENCE:
            j, body_lines = start, []
            while j < n and lines[j].strip() != FENCE:
                body_lines.append(lines[j])
                j += 1
            if j >= n:
                raise TmdlError(f"unterminated ``` block starting at line {start}")
            return _dedent(body_lines), j + 1
        j, body_lines = start, []
        while j < n:
            ln = lines[j]
            if ln.strip() and _indent_of(ln) < min_indent:
                break
            body_lines.append(ln)
            j += 1
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
            j -= 1
        cont = _dedent(body_lines)
        if first and cont:
            return first + "\n" + cont, j
        return (first or cont), j

    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            pending_desc = []
            i += 1
            continue
        if stripped.startswith("///"):
            pending_desc.append(stripped[3:].removeprefix(" "))
            i += 1
            continue
        if stripped.startswith("//"):
            i += 1
            continue

        indent = _indent_of(raw)
        while stack and stack[-1].indent >= indent:
            stack.pop()
        parent = stack[-1] if stack else None
        keyword, _, rest = stripped.partition(" ")
        is_object_level = parent is None or indent == parent.indent + 1

        if keyword in OBJECT_KEYWORDS and is_object_level and not keyword.endswith(":"):
            if keyword == "ref":
                obj = TmdlObject("ref", rest.strip(), i, indent)
                remainder = ""
            else:
                name, remainder = _parse_name(rest) if rest else ("", "")
                obj = TmdlObject(keyword, name, i, indent)
            obj.description = "\n".join(pending_desc) if pending_desc else None
            pending_desc = []
            remainder = remainder.strip()
            i += 1
            if remainder.startswith("="):
                expr, i = collect_expression(i, remainder[1:], indent + 2)
                obj.expression = expr
            (parent.children if parent else objects).append(obj)
            stack.append(obj)
            continue

        pending_desc = []
        if parent is not None and indent == parent.indent + 1:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*(:|=)?\s*(.*)$", stripped)
            if m:
                key, sep, value = m.group(1), m.group(2), m.group(3)
                if sep == "=":
                    expr, i = collect_expression(i + 1, value, indent + 2)
                    parent.properties[key] = expr
                    continue
                parent.properties[key] = value if sep == ":" else "true"
        i += 1
    return TmdlDocument(text=text, objects=objects)


# ---------------------------------------------------------------- semantic model view


def read_text(path: Path) -> str:
    """Read a TMDL file exactly: no newline translation; a BOM stays as U+FEFF."""
    return path.read_bytes().decode("utf-8")


def _flag(obj: TmdlObject, key: str) -> bool:
    return obj.properties.get(key, "false") == "true"


def parse_column_ref(ref: str) -> tuple[str, str]:
    """`Sales.ProductKey` or `'Sales Table'.'Product Key'` -> (table, column)."""
    ref = ref.strip()
    if ref.startswith("'"):
        table, rest = _parse_name(ref)
    else:
        table, dot, rest = ref.partition(".")
        rest = dot + rest
    if not table or not rest.startswith("."):
        raise TmdlError(f"not a column reference: {ref!r}")
    column = rest[1:].strip()
    if column.startswith("'"):
        column, _ = _parse_name(column)
    if not column:
        raise TmdlError(f"not a column reference: {ref!r}")
    return table, column


def _column(c: TmdlObject, calculated_table: bool) -> Column:
    kind = (
        "calculated"
        if c.expression is not None
        else ("calculatedTableColumn" if calculated_table else "data")
    )
    return Column(
        name=c.name,
        data_type=c.properties.get("dataType"),
        kind=kind,
        description=c.description,
        is_hidden=_flag(c, "isHidden"),
        is_key=_flag(c, "isKey"),
        summarize_by=c.properties.get("summarizeBy"),
        source_column=c.properties.get("sourceColumn"),
        expression=c.expression,
        format_string=c.properties.get("formatString"),
        data_category=c.properties.get("dataCategory"),
        sort_by_column=c.properties.get("sortByColumn"),
        display_folder=c.properties.get("displayFolder"),
    )


def _table(obj: TmdlObject, file: str) -> Table:
    partitions = [
        Partition(p.name, p.expression, p.properties.get("mode"), p.properties.get("source"))
        for p in obj.children
        if p.kind == "partition"
    ]
    calculated = any(p.kind == "calculated" for p in partitions)
    hierarchies = [
        Hierarchy(
            h.name,
            [lv.properties.get("column", lv.name) for lv in h.children if lv.kind == "level"],
        )
        for h in obj.children
        if h.kind == "hierarchy"
    ]
    return Table(
        name=obj.name,
        file=file,
        description=obj.description,
        is_hidden=_flag(obj, "isHidden"),
        data_category=obj.properties.get("dataCategory"),
        columns=[_column(c, calculated) for c in obj.children if c.kind == "column"],
        measures=[
            Measure(
                name=m.name,
                expression=m.expression or "",
                description=m.description,
                format_string=m.properties.get("formatString"),
                display_folder=m.properties.get("displayFolder"),
                is_hidden=_flag(m, "isHidden"),
            )
            for m in obj.children
            if m.kind == "measure"
        ],
        partitions=partitions,
        hierarchies=hierarchies,
    )


def _relationship(obj: TmdlObject) -> Relationship | None:
    try:
        ft, fc = parse_column_ref(obj.properties.get("fromColumn", ""))
        tt, tc = parse_column_ref(obj.properties.get("toColumn", ""))
    except TmdlError:
        return None
    return Relationship(
        name=obj.name,
        from_table=ft,
        from_column=fc,
        to_table=tt,
        to_column=tc,
        from_cardinality=obj.properties.get("fromCardinality", "many"),
        to_cardinality=obj.properties.get("toCardinality", "one"),
        cross_filtering=obj.properties.get("crossFilteringBehavior", "oneDirection"),
        is_active=obj.properties.get("isActive", "true") != "false",
    )


def load_model(name: str, definition_dir: Path) -> SemanticModel:
    tables: dict[str, Table] = {}
    for path in sorted((definition_dir / "tables").glob("*.tmdl")):
        rel = path.relative_to(definition_dir).as_posix()
        for obj in parse(read_text(path)).objects:
            if obj.kind == "table":
                tables[obj.name] = _table(obj, rel)
    relationships: list[Relationship] = []
    rel_path = definition_dir / "relationships.tmdl"
    if rel_path.exists():
        for obj in parse(read_text(rel_path)).objects:
            r = _relationship(obj) if obj.kind == "relationship" else None
            if r is not None:
                relationships.append(r)
    annotations: dict[str, str] = {}
    model_path = definition_dir / "model.tmdl"
    if model_path.exists():
        for obj in parse(read_text(model_path)).objects:
            nested = obj.children if obj.kind == "model" else [obj]
            for a in nested:
                if a.kind == "annotation":
                    annotations[a.name] = (a.expression or "").strip()
    expressions: dict[str, str] = {}
    expr_path = definition_dir / "expressions.tmdl"
    if expr_path.exists():
        for obj in parse(read_text(expr_path)).objects:
            if obj.kind == "expression":
                expressions[obj.name] = obj.expression or ""
    return SemanticModel(
        name, "pbip", tables, relationships, annotations, expressions, definition_dir
    )


# ---------------------------------------------------------------- editing


def clean_description(description: str) -> str:
    text = description.replace("\r", "").strip()
    lines = [ln.strip() for ln in text.split("\n")]
    if any(FENCE in ln for ln in lines):
        raise TmdlError("description may not contain ```")
    return "\n".join(lines)


def set_description(
    text: str, kind: str, name: str, description: str | None, table: str | None = None
) -> str:
    """Return `text` with the `///` block above the object replaced (or removed when empty).

    `kind`/`name` identify the object; `table` is required for columns and measures.
    """
    doc = parse(text)
    parent: tuple[str, str] | None = None
    if kind in ("column", "measure"):
        if table is None:
            raise TmdlError(f"a {kind} needs its table")
        parent = ("table", table)
    obj = doc.find(kind, name, parent)
    if obj is None:
        where = f" in table {table!r}" if table else ""
        raise TmdlError(f"{kind} {name!r} not found{where}")

    bom = "\ufeff" if text.startswith("\ufeff") else ""
    body = text[len(bom) :]
    newline = "\r\n" if "\r\n" in body else "\n"
    lines = body.splitlines(keepends=True)
    decl = lines[obj.line]
    lead = decl[: len(decl) - len(decl.lstrip("\t "))]
    start = obj.line
    while (
        start > 0
        and lines[start - 1].lstrip("\t ").startswith("///")
        and lines[start - 1].startswith(lead)
        and _indent_of(lines[start - 1]) == obj.indent
    ):
        start -= 1
    new_block = []
    if description:
        for ln in clean_description(description).split("\n"):
            new_block.append(f"{lead}/// {ln}".rstrip() + newline)
    return bom + "".join(lines[:start] + new_block + lines[obj.line :])


def apply_set_description(text: str, intent: dict[str, Any]) -> str:
    return set_description(
        text, intent["kind"], intent["name"], intent.get("value"), table=intent.get("table")
    )


def check_set_description(before: str, after: str, intents: list[dict[str, Any]]) -> list[str]:
    """Structural check: every intended description is present, nothing else changed."""
    problems: list[str] = []
    try:
        b, a = parse(before), parse(after)
    except TmdlError as e:
        return [f"TMDL no longer parses: {e}"]
    targets = {(i.get("table"), i["kind"], i["name"]): i.get("value") for i in intents}
    for (table, kind, name), value in targets.items():
        parent = ("table", table) if table else None
        obj = a.find(kind, name, parent)
        if obj is None:
            problems.append(f"{kind} {name!r} missing after edit")
        elif (obj.description or None) != (clean_description(value) if value else None):
            problems.append(f"{kind} {name!r} description is not the proposed text")

    def strip(doc: TmdlDocument) -> Any:
        out = []
        for o in doc.objects:
            s = o.structure()
            if o.kind == "table":
                kids = []
                for c in o.children:
                    skip = (c.kind, c.name) if (o.name, c.kind, c.name) in targets else None
                    kids.append(c.structure(skip))
                desc = None if (None, "table", o.name) in targets else o.description
                s = (
                    o.kind,
                    o.name,
                    desc,
                    o.expression,
                    tuple(sorted(o.properties.items())),
                    tuple(kids),
                )
            out.append(s)
        return out

    if strip(b) != strip(a):
        problems.append("the edit changed something other than the targeted descriptions")
    return problems
