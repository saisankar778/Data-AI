"""Read a `.pbix` file's model metadata with pbixray. Metadata only: this module never calls
pbixray's table-data functions (`get_table`, `iter_table`).

pbixray's public frames lack a few fields we need (measure format strings, hidden flags, sort-by
columns, inferred types of calculated columns), so we query the engine's metadata tables through
pbixray's reader. The pbixray version is pinned (ADR 0004); `test_pbix.py` guards this."""

from __future__ import annotations

import math
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

# Tabular Object Model enum values, as stored in the model's metadata database.
DATA_TYPES = {
    2: "string",
    6: "int64",
    8: "double",
    9: "dateTime",
    10: "decimal",
    11: "boolean",
    17: "binary",
    20: "variant",
}
SUMMARIZE_BY = {
    1: "default",
    2: "none",
    3: "sum",
    4: "min",
    5: "max",
    6: "count",
    7: "average",
    8: "distinctCount",
}
COLUMN_KINDS = {1: "data", 2: "calculated", 3: "rowNumber", 4: "calculatedTableColumn"}
PARTITION_KINDS = {
    1: "query",
    2: "calculated",
    3: "none",
    4: "m",
    5: "entity",
    6: "policyRange",
    7: "calculationGroup",
    8: "inferred",
    9: "parquet",
}
MODES = {0: "import", 1: "directQuery", 2: "default", 3: "push", 4: "dual", 5: "directLake"}
CARDINALITY = {1: "one", 2: "many"}
CROSS_FILTER = {1: "oneDirection", 2: "bothDirections", 3: "automatic"}


class PbixError(ValueError):
    pass


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _text(value: Any) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    s = str(value)
    return s if s.strip() else None


def _int(value: Any, default: int = 0) -> int:
    value = _clean(value)
    return default if value is None else int(value)


class _Reader:
    def __init__(self, path: Path) -> None:
        from pbixray import PBIXRay

        try:
            self.pbix = PBIXRay(str(path))
        except Exception as e:  # pbixray raises several types for damaged or unsupported files
            raise PbixError(f"could not read {path.name}: {e}") from e
        self._db = self.pbix._metadata.source._db

    def rows(self, sql: str) -> list[dict[str, Any]]:
        frame = self._db.query(sql)
        records: list[dict[str, Any]] = frame.to_dict("records")
        return [{k: _clean(v) for k, v in r.items()} for r in records]


def load_pbix_model(path: Path) -> SemanticModel:
    reader = _Reader(path)
    try:
        return _load(reader, path.stem)
    finally:
        reader.pbix.close()


def _load(r: _Reader, name: str) -> SemanticModel:
    tables_by_id: dict[int, Table] = {}
    # SystemFlags bit 1 marks the engine's own storage tables (H$, R$, U$); skip those.
    for t in r.rows(
        "SELECT ID, Name, Description, IsHidden, DataCategory FROM [Table] "
        "WHERE SystemFlags IS NULL OR (SystemFlags % 2) = 0"
    ):
        tables_by_id[_int(t["ID"])] = Table(
            name=str(t["Name"]),
            description=_text(t["Description"]),
            is_hidden=bool(_int(t["IsHidden"])),
            data_category=_text(t["DataCategory"]),
        )

    column_names: dict[int, tuple[int, str]] = {}
    raw_columns = r.rows(
        "SELECT ID, TableID, ExplicitName, InferredName, ExplicitDataType, InferredDataType, "
        "DataCategory, Description, IsHidden, IsKey, SummarizeBy, Type, SourceColumn, Expression, "
        "FormatString, SortByColumnID, DisplayFolder FROM [Column]"
    )
    for c in raw_columns:
        column_names[_int(c["ID"])] = (
            _int(c["TableID"]),
            _text(c["ExplicitName"]) or _text(c["InferredName"]) or "",
        )
    for c in raw_columns:
        kind = COLUMN_KINDS.get(_int(c["Type"]), "data")
        table = tables_by_id.get(_int(c["TableID"]))
        if table is None or kind == "rowNumber":
            continue
        dtype = _int(c["ExplicitDataType"])
        if dtype in (0, 1, 19):  # automatic or unknown: use what the engine inferred
            dtype = _int(c["InferredDataType"])
        sort_by = column_names.get(_int(c["SortByColumnID"], -1))
        table.columns.append(
            Column(
                name=column_names[_int(c["ID"])][1],
                data_type=DATA_TYPES.get(dtype),
                kind=kind,
                description=_text(c["Description"]),
                is_hidden=bool(_int(c["IsHidden"])),
                is_key=bool(_int(c["IsKey"])),
                summarize_by=SUMMARIZE_BY.get(_int(c["SummarizeBy"])),
                source_column=_text(c["SourceColumn"]),
                expression=_text(c["Expression"]),
                format_string=_text(c["FormatString"]),
                data_category=_text(c["DataCategory"]),
                sort_by_column=sort_by[1] if sort_by else None,
                display_folder=_text(c["DisplayFolder"]),
            )
        )

    for m in r.rows(
        "SELECT TableID, Name, Expression, Description, FormatString, IsHidden, "
        "DisplayFolder FROM [Measure]"
    ):
        table = tables_by_id.get(_int(m["TableID"]))
        if table is not None:
            table.measures.append(
                Measure(
                    name=str(m["Name"]),
                    expression=_text(m["Expression"]) or "",
                    description=_text(m["Description"]),
                    format_string=_text(m["FormatString"]),
                    display_folder=_text(m["DisplayFolder"]),
                    is_hidden=bool(_int(m["IsHidden"])),
                )
            )

    for p in r.rows("SELECT TableID, Name, Type, Mode, QueryDefinition FROM [Partition]"):
        table = tables_by_id.get(_int(p["TableID"]))
        if table is not None:
            table.partitions.append(
                Partition(
                    name=str(p["Name"]),
                    kind=PARTITION_KINDS.get(_int(p["Type"])),
                    mode=MODES.get(_int(p["Mode"], -1)),
                    source=_text(p["QueryDefinition"]),
                )
            )

    hierarchies: dict[int, Hierarchy] = {}
    for row in r.rows("SELECT ID, TableID, Name FROM [Hierarchy]"):
        table = tables_by_id.get(_int(row["TableID"]))
        if table is not None:
            hierarchies[_int(row["ID"])] = Hierarchy(str(row["Name"]))
            table.hierarchies.append(hierarchies[_int(row["ID"])])
    for lv in r.rows("SELECT HierarchyID, ColumnID, Ordinal FROM [Level] ORDER BY Ordinal"):
        hierarchy = hierarchies.get(_int(lv["HierarchyID"]))
        col = column_names.get(_int(lv["ColumnID"]))
        if hierarchy is not None and col is not None:
            hierarchy.levels.append(col[1])

    relationships = []
    for rel in r.rows(
        "SELECT ID, Name, IsActive, FromColumnID, FromCardinality, ToColumnID, ToCardinality, "
        "CrossFilteringBehavior FROM [Relationship]"
    ):
        frm, to = (
            column_names.get(_int(rel["FromColumnID"])),
            column_names.get(_int(rel["ToColumnID"])),
        )
        if frm is None or to is None:
            continue
        ft, tt = tables_by_id.get(frm[0]), tables_by_id.get(to[0])
        if ft is None or tt is None:
            continue
        relationships.append(
            Relationship(
                name=_text(rel["Name"]) or str(rel["ID"]),
                from_table=ft.name,
                from_column=frm[1],
                to_table=tt.name,
                to_column=to[1],
                from_cardinality=CARDINALITY.get(_int(rel["FromCardinality"]), "many"),
                to_cardinality=CARDINALITY.get(_int(rel["ToCardinality"]), "one"),
                cross_filtering=CROSS_FILTER.get(
                    _int(rel["CrossFilteringBehavior"]), "oneDirection"
                ),
                is_active=bool(_int(rel["IsActive"], 1)),
            )
        )

    annotations = {
        str(a["Name"]): str(a["Value"])
        for a in r.rows("SELECT Name, Value FROM [Annotation] WHERE ObjectType = 1")
    }
    expressions = {
        str(e["Name"]): _text(e["Expression"]) or ""
        for e in r.rows("SELECT Name, Expression FROM [Expression]")
    }
    return SemanticModel(
        name=name,
        source="pbix",
        tables={t.name: t for t in tables_by_id.values()},
        relationships=relationships,
        annotations=annotations,
        expressions=expressions,
    )
