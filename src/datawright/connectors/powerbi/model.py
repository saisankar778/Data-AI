"""Source-neutral semantic model. Both the TMDL (PBIP) and `.pbix` readers produce these, so the
rules, documenter and MCP tools work the same on either. Vocabulary follows TMDL."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from datawright.connectors.powerbi.report import ReportUsage

AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")


@dataclass
class Column:
    name: str
    data_type: str | None = None  # string, int64, double, decimal, dateTime, boolean, binary
    kind: str = "data"  # data | calculated | calculatedTableColumn
    description: str | None = None
    is_hidden: bool = False
    is_key: bool = False
    summarize_by: str | None = None  # none, sum, count, min, max, average, distinctCount, default
    source_column: str | None = None
    expression: str | None = None
    format_string: str | None = None
    data_category: str | None = None
    sort_by_column: str | None = None
    display_folder: str | None = None


@dataclass
class Measure:
    name: str
    expression: str
    description: str | None = None
    format_string: str | None = None
    display_folder: str | None = None
    is_hidden: bool = False


@dataclass
class Partition:
    name: str
    kind: str | None  # m | calculated | entity | query | ...
    mode: str | None = None
    source: str | None = None


@dataclass
class Hierarchy:
    name: str
    levels: list[str] = field(default_factory=list)  # column names


@dataclass
class Table:
    name: str
    file: str | None = None  # TMDL file relative to the definition folder
    description: str | None = None
    is_hidden: bool = False
    data_category: str | None = None
    columns: list[Column] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    partitions: list[Partition] = field(default_factory=list)
    hierarchies: list[Hierarchy] = field(default_factory=list)

    def measure(self, name: str) -> Measure | None:
        return next((m for m in self.measures if m.name == name), None)

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def is_calculated(self) -> bool:
        return any(p.kind == "calculated" for p in self.partitions)

    @property
    def is_auto_date(self) -> bool:
        return self.name.startswith(AUTO_DATE_PREFIXES)

    @property
    def is_field_parameter(self) -> bool:
        """Field parameter tables are calculated tables built from NAMEOF(...) references."""
        return any(
            p.kind == "calculated" and "NAMEOF(" in (p.source or "").upper()
            for p in self.partitions
        )


@dataclass
class Relationship:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_cardinality: str = "many"
    to_cardinality: str = "one"
    cross_filtering: str = "oneDirection"  # oneDirection | bothDirections | automatic
    is_active: bool = True

    @property
    def label(self) -> str:
        return f"{self.from_table}[{self.from_column}] -> {self.to_table}[{self.to_column}]"


@dataclass
class SemanticModel:
    name: str
    source: str  # pbip | pbix
    tables: dict[str, Table]
    relationships: list[Relationship]
    annotations: dict[str, str] = field(default_factory=dict)
    expressions: dict[str, str] = field(default_factory=dict)  # shared M queries and parameters
    definition_dir: Path | None = None
    report_usage: ReportUsage | None = None  # None: no report to check against

    def all_measures(self) -> list[tuple[Table, Measure]]:
        return [(t, m) for t in self.tables.values() for m in t.measures]

    def all_columns(self) -> list[tuple[Table, Column]]:
        return [(t, c) for t in self.tables.values() for c in t.columns]

    def measure_names(self) -> set[str]:
        return {m.name for _, m in self.all_measures()}
