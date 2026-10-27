"""Power BI connectors: PBIP folders in Git (read/propose/apply) and `.pbix` files (read only).

Both expose the same SemanticModel, asset crawl and object lookup, so everything above this
layer works on either source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from datawright.connectors.base import AssetRecord
from datawright.connectors.powerbi.model import Column, Measure, SemanticModel, Table
from datawright.connectors.powerbi.tmdl import load_model
from datawright.core import gitops
from datawright.core.capabilities import Capability
from datawright.core.paths import make_path, split_path

SUFFIX = ".SemanticModel"


def find_models(root: Path) -> list[tuple[str, Path]]:
    """(model name, definition dir) for every `<name>.SemanticModel/definition` under root."""
    found = []
    for definition in sorted(root.rglob(f"*{SUFFIX}/definition")):
        if ".git" in definition.parts or not definition.is_dir():
            continue
        found.append((definition.parent.name[: -len(SUFFIX)], definition))
    return found


@dataclass
class ObjectRef:
    """A table, column or measure found by its object path."""

    kind: str  # table | column | measure
    model: SemanticModel
    table: Table
    object_path: str
    file_path: str | None  # TMDL file relative to the connection root (None for .pbix)
    column: Column | None = None
    measure: Measure | None = None

    @property
    def name(self) -> str:
        if self.measure is not None:
            return self.measure.name
        if self.column is not None:
            return self.column.name
        return self.table.name

    @property
    def description(self) -> str | None:
        if self.measure is not None:
            return self.measure.description
        if self.column is not None:
            return self.column.description
        return self.table.description

    @property
    def is_hidden(self) -> bool:
        if self.measure is not None:
            return self.measure.is_hidden or self.table.is_hidden
        if self.column is not None:
            return self.column.is_hidden or self.table.is_hidden
        return self.table.is_hidden


class _PowerBIConnector:
    kind: ClassVar[str]
    max_capability: ClassVar[Capability]
    root: Path

    def models(self) -> dict[str, SemanticModel]:
        raise NotImplementedError

    def file_for(self, model: SemanticModel, table: Table) -> str | None:
        if model.definition_dir is None or table.file is None:
            return None
        return (model.definition_dir / table.file).relative_to(self.root).as_posix()

    def objects(self) -> list[ObjectRef]:
        out = []
        for model in self.models().values():
            for t in model.tables.values():
                f = self.file_for(model, t)
                out.append(ObjectRef("table", model, t, make_path(model.name, t.name), f))
                for c in t.columns:
                    out.append(
                        ObjectRef(
                            "column",
                            model,
                            t,
                            make_path(model.name, t.name, "column", c.name),
                            f,
                            column=c,
                        )
                    )
                for m in t.measures:
                    out.append(
                        ObjectRef(
                            "measure",
                            model,
                            t,
                            make_path(model.name, t.name, "measure", m.name),
                            f,
                            measure=m,
                        )
                    )
        return out

    def resolve(self, object_path: str) -> ObjectRef:
        parts = split_path(object_path)
        model = self.models().get(parts[0]) if parts else None
        table = model.tables.get(parts[1]) if model and len(parts) > 1 else None
        if model is None or table is None:
            raise KeyError(f"object not found: {object_path!r}")
        f = self.file_for(model, table)
        if len(parts) == 2:
            return ObjectRef("table", model, table, object_path, f)
        if len(parts) == 4 and parts[2] == "column":
            c = table.column(parts[3])
            if c is not None:
                return ObjectRef("column", model, table, object_path, f, column=c)
        if len(parts) == 4 and parts[2] == "measure":
            m = table.measure(parts[3])
            if m is not None:
                return ObjectRef("measure", model, table, object_path, f, measure=m)
        raise KeyError(f"object not found: {object_path!r}")

    def crawl(self) -> list[AssetRecord]:
        out: list[AssetRecord] = []
        for model in self.models().values():
            out.append(
                AssetRecord(
                    "semantic_model",
                    make_path(model.name),
                    model.name,
                    meta={"source": model.source},
                )
            )
        for o in self.objects():
            meta: dict[str, object] = {"hidden": o.is_hidden}
            if o.column is not None:
                meta |= {
                    "data_type": o.column.data_type,
                    "kind": o.column.kind,
                    "summarize_by": o.column.summarize_by,
                }
            if o.measure is not None:
                meta |= {
                    "expression": o.measure.expression,
                    "format_string": o.measure.format_string,
                    "display_folder": o.measure.display_folder,
                }
            out.append(AssetRecord(o.kind, o.object_path, o.name, o.file_path, o.description, meta))
        for model in self.models().values():
            for r in model.relationships:
                out.append(
                    AssetRecord(
                        "relationship",
                        make_path(model.name, "relationship", r.name),
                        r.label,
                        None,
                        None,
                        {
                            "cross_filtering": r.cross_filtering,
                            "active": r.is_active,
                            "from_cardinality": r.from_cardinality,
                            "to_cardinality": r.to_cardinality,
                        },
                    )
                )
        return out


class PbipRepoConnector(_PowerBIConnector):
    kind: ClassVar[str] = "pbip_repo"
    max_capability: ClassVar[Capability] = Capability.APPLY

    def __init__(self, root: Path) -> None:
        self.root = root
        self._models: dict[str, SemanticModel] | None = None

    def check(self) -> list[str]:
        if not self.root.is_dir():
            return [f"{self.root} is not a folder"]
        problems = []
        if gitops.project_root(self.root) is None:
            problems.append(
                f"{self.root} is not in its own Git repository (run `datawright track`)"
            )
        if not find_models(self.root):
            problems.append(f"no *{SUFFIX}/definition folder found under {self.root}")
        return problems

    def models(self) -> dict[str, SemanticModel]:
        if self._models is None:
            self._models = {name: load_model(name, d) for name, d in find_models(self.root)}
        return self._models


class PbixFileConnector(_PowerBIConnector):
    """A `.pbix` file. Read only: Datawright never rewrites a .pbix. To make changes, save the
    report as a Power BI Project (PBIP) in Desktop and track that folder with Git."""

    kind: ClassVar[str] = "pbix_file"
    max_capability: ClassVar[Capability] = Capability.READ

    def __init__(self, path: Path) -> None:
        self.root = path
        self._models: dict[str, SemanticModel] | None = None

    def check(self) -> list[str]:
        if not self.root.is_file() or self.root.suffix.lower() != ".pbix":
            return [f"{self.root} is not a .pbix file"]
        return []

    def models(self) -> dict[str, SemanticModel]:
        if self._models is None:
            from datawright.connectors.powerbi.pbix import load_pbix_model

            model = load_pbix_model(self.root)
            self._models = {model.name: model}
        return self._models


PowerBIConnector = PbipRepoConnector | PbixFileConnector


def open_source(path: Path) -> PowerBIConnector:
    """A connector for a PBIP folder or a .pbix file (for checks that need no Git)."""
    if path.is_file() and path.suffix.lower() == ".pbix":
        return PbixFileConnector(path)
    return PbipRepoConnector(path)
