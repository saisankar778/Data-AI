"""PBIP folders in a Git repository: find semantic models, crawl metadata, resolve object paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from datawright.connectors.base import AssetRecord
from datawright.connectors.powerbi.tmdl import Measure, SemanticModel, Table, load_model
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
class MeasureRef:
    model: SemanticModel
    table: Table
    measure: Measure
    object_path: str
    file_path: str  # relative to the connection root


class PbipRepoConnector:
    kind: ClassVar[str] = "pbip_repo"
    max_capability: ClassVar[Capability] = Capability.APPLY

    def __init__(self, root: Path) -> None:
        self.root = root
        self._models: dict[str, SemanticModel] | None = None

    def check(self) -> list[str]:
        problems = []
        if not self.root.is_dir():
            return [f"{self.root} is not a folder"]
        if not gitops.is_repo(self.root):
            problems.append(
                f"{self.root} is not a Git repository (run `git init` and commit first)"
            )
        if not find_models(self.root):
            problems.append(f"no *{SUFFIX}/definition folder found under {self.root}")
        return problems

    def models(self) -> dict[str, SemanticModel]:
        if self._models is None:
            self._models = {name: load_model(name, d) for name, d in find_models(self.root)}
        return self._models

    def rel(self, model: SemanticModel, file: str) -> str:
        return (model.definition_dir / file).relative_to(self.root).as_posix()

    def crawl(self) -> list[AssetRecord]:
        out: list[AssetRecord] = []
        for model in self.models().values():
            out.append(
                AssetRecord(
                    "semantic_model",
                    make_path(model.name),
                    model.name,
                    model.definition_dir.relative_to(self.root).as_posix(),
                )
            )
            for table in model.tables.values():
                file = self.rel(model, table.file)
                out.append(
                    AssetRecord(
                        "table",
                        make_path(model.name, table.name),
                        table.name,
                        file,
                        table.description,
                        {"hidden": table.is_hidden},
                    )
                )
                for c in table.columns:
                    out.append(
                        AssetRecord(
                            "column",
                            make_path(model.name, table.name, "column", c.name),
                            c.name,
                            file,
                            c.description,
                            {
                                "data_type": c.data_type,
                                "hidden": c.is_hidden,
                                "summarize_by": c.summarize_by,
                            },
                        )
                    )
                for m in table.measures:
                    out.append(
                        AssetRecord(
                            "measure",
                            make_path(model.name, table.name, "measure", m.name),
                            m.name,
                            file,
                            m.description,
                            {
                                "expression": m.expression,
                                "format_string": m.format_string,
                                "display_folder": m.display_folder,
                            },
                        )
                    )
            for r in model.relationships:
                out.append(
                    AssetRecord(
                        "relationship",
                        make_path(model.name, "relationship", r.name),
                        r.name,
                        self.rel(model, "relationships.tmdl"),
                        None,
                        {
                            "from": r.from_column,
                            "to": r.to_column,
                            "cross_filtering": r.cross_filtering,
                            "active": r.is_active,
                        },
                    )
                )
        return out

    def measures(self) -> list[MeasureRef]:
        refs = []
        for model in self.models().values():
            for table in model.tables.values():
                for m in table.measures:
                    refs.append(
                        MeasureRef(
                            model,
                            table,
                            m,
                            make_path(model.name, table.name, "measure", m.name),
                            self.rel(model, table.file),
                        )
                    )
        return refs

    def measure(self, object_path: str) -> MeasureRef:
        parts = split_path(object_path)
        if len(parts) != 4 or parts[2] != "measure":
            raise KeyError(f"not a measure path: {object_path!r}")
        model = self.models().get(parts[0])
        table = model.tables.get(parts[1]) if model else None
        measure = table.measure(parts[3]) if table else None
        if model is None or table is None or measure is None:
            raise KeyError(f"measure not found: {object_path!r}")
        return MeasureRef(model, table, measure, object_path, self.rel(model, table.file))
