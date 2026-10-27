"""Semantic diff of two versions of a model: what changed, what it breaks, and the health delta.

Versions come from Git revisions (or the working tree), so a branch, a pull request or a
change set can be explained in model terms instead of TMDL line noise.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from datawright.connectors.powerbi.health import HealthReport, health
from datawright.connectors.powerbi.model import Relationship, SemanticModel
from datawright.connectors.powerbi.pbip import SUFFIX, PbipRepoConnector
from datawright.connectors.powerbi.rules import RuleFinding
from datawright.core import gitops
from datawright.core.paths import make_path, split_path

COLUMN_FIELDS = ("data_type", "description", "is_hidden", "is_key", "summarize_by",
                 "sort_by_column", "data_category", "format_string", "expression",
                 "source_column", "display_folder")  # fmt: skip
MEASURE_FIELDS = ("expression", "description", "format_string", "display_folder", "is_hidden")
TABLE_FIELDS = ("description", "is_hidden", "data_category")
REL_FIELDS = ("is_active", "cross_filtering", "from_cardinality", "to_cardinality")


@dataclass
class Change:
    kind: str  # table | column | measure | relationship
    object_path: str
    change: str  # added | removed | changed
    fields: list[tuple[str, Any, Any]] = field(default_factory=list)  # (field, old, new)


@dataclass
class ModelDiff:
    model: str
    changes: list[Change] = field(default_factory=list)
    old_health: HealthReport | None = None
    new_health: HealthReport | None = None
    new_findings: list[RuleFinding] = field(default_factory=list)
    resolved_findings: list[RuleFinding] = field(default_factory=list)
    broken_visuals: list[str] = field(default_factory=list)  # removed fields visuals still use

    @property
    def broken_references(self) -> list[RuleFinding]:
        return [f for f in self.new_findings if f.rule == "unknown-reference"]

    @property
    def score_delta(self) -> int:
        old = self.old_health.score if self.old_health else 0
        new = self.new_health.score if self.new_health else 0
        return new - old


def _rel_key(r: Relationship) -> tuple[str, str, str, str]:
    return (r.from_table, r.from_column, r.to_table, r.to_column)


def _fields(old: Any, new: Any, names: tuple[str, ...]) -> list[tuple[str, Any, Any]]:
    return [(n, getattr(old, n), getattr(new, n)) for n in names
            if getattr(old, n) != getattr(new, n)]  # fmt: skip


def diff_models(old: SemanticModel | None, new: SemanticModel | None) -> ModelDiff:
    name = (new or old).name if (new or old) else "?"  # type: ignore[union-attr]
    d = ModelDiff(name)
    old_tables = old.tables if old else {}
    new_tables = new.tables if new else {}
    for tname in sorted(set(old_tables) | set(new_tables)):
        ot, nt = old_tables.get(tname), new_tables.get(tname)
        tpath = make_path(name, tname)
        if ot is None or nt is None:
            d.changes.append(Change("table", tpath, "added" if ot is None else "removed"))
            continue
        if f := _fields(ot, nt, TABLE_FIELDS):
            d.changes.append(Change("table", tpath, "changed", f))
        for kind, olds, news, names in (
            ("column", {c.name: c for c in ot.columns}, {c.name: c for c in nt.columns},
             COLUMN_FIELDS),
            ("measure", {m.name: m for m in ot.measures}, {m.name: m for m in nt.measures},
             MEASURE_FIELDS),
        ):  # fmt: skip
            for oname in sorted(set(olds) | set(news)):
                o, n = olds.get(oname), news.get(oname)
                path = make_path(name, tname, kind, oname)
                if o is None or n is None:
                    d.changes.append(Change(kind, path, "added" if o is None else "removed"))
                elif f := _fields(o, n, names):
                    d.changes.append(Change(kind, path, "changed", f))
    old_rels = {_rel_key(r): r for r in (old.relationships if old else [])}
    new_rels = {_rel_key(r): r for r in (new.relationships if new else [])}
    for key in sorted(set(old_rels) | set(new_rels)):
        orel, nrel = old_rels.get(key), new_rels.get(key)
        path = make_path(name, "relationship", f"{key[0]}[{key[1]}] -> {key[2]}[{key[3]}]")
        if orel is None or nrel is None:
            d.changes.append(Change("relationship", path, "added" if orel is None else "removed"))
        elif f := _fields(orel, nrel, REL_FIELDS):
            d.changes.append(Change("relationship", path, "changed", f))
    usage = old.report_usage if old else None
    if usage is not None:
        for c in d.changes:
            if c.change != "removed" or c.kind not in ("table", "column", "measure"):
                continue
            parts = split_path(c.object_path)
            if c.kind == "table":
                keys = [k for k in usage.uses if k[0] == parts[1].casefold()]
            else:
                keys = [(parts[1].casefold(), parts[3].casefold())]
            places = sorted({p for k in keys for p in usage.uses.get(k, [])})
            if places:
                shown = "; ".join(places[:5]) + (f" (+{len(places) - 5} more)" if len(places) > 5
                                                 else "")  # fmt: skip
                d.broken_visuals.append(f"{c.kind} {c.object_path} is used by {shown}")
    d.old_health = health(old) if old else None
    d.new_health = health(new) if new else None
    old_set = {(f.rule, f.object_path) for f in (d.old_health.findings if d.old_health else [])}
    new_set = {(f.rule, f.object_path) for f in (d.new_health.findings if d.new_health else [])}
    d.new_findings = [f for f in (d.new_health.findings if d.new_health else [])
                      if (f.rule, f.object_path) not in old_set]  # fmt: skip
    d.resolved_findings = [f for f in (d.old_health.findings if d.old_health else [])
                           if (f.rule, f.object_path) not in new_set]  # fmt: skip
    return d


def _is_model_file(path: str) -> bool:
    if f"{SUFFIX}/definition/" in path and path.endswith(".tmdl"):
        return True
    return ".Report/" in path and path.endswith((".json", ".pbir"))


def models_at(repo: Path, rev: str | None) -> dict[str, SemanticModel]:
    """All models (with their reports' field usage) in `repo` at Git revision `rev`
    (None = the working tree)."""
    if rev is None:
        return PbipRepoConnector(repo).models()
    files = [f for f in gitops.out(repo, "ls-tree", "-r", "--name-only", rev).splitlines()
             if _is_model_file(f)]  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for f in files:
            target = root / f
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(gitops.show(repo, rev, f))
        return PbipRepoConnector(root).models()


def models_with_edits(repo: Path, edits: dict[str, str]) -> dict[str, SemanticModel]:
    """The working-tree models with some files replaced (to preview a change set)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for src in repo.rglob("*"):
            rel = src.relative_to(repo).as_posix()
            if src.is_file() and ".git/" not in rel and _is_model_file(rel):
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read_bytes())
        for rel_path, text in edits.items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        return PbipRepoConnector(root).models()


def diff_revisions(repo: Path, base: str | None, head: str | None) -> list[ModelDiff]:
    old, new = models_at(repo, base), models_at(repo, head)
    return [diff_models(old.get(n), new.get(n)) for n in sorted(set(old) | set(new))]


def _short(value: Any, limit: int = 70) -> str:
    text = " ".join(str(value).split()) if value is not None else "(none)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(diffs: list[ModelDiff], title: str = "Model changes") -> str:
    lines = [f"## {title}", ""]
    for d in diffs:
        old = d.old_health.score if d.old_health else None
        new = d.new_health.score if d.new_health else None
        sign = "+" if d.score_delta > 0 else ""
        lines.append(f"### {d.model}: health {old} -> {new} ({sign}{d.score_delta})")
        lines.append("")
        if d.broken_visuals:
            lines.append(f"**Breaks {len(d.broken_visuals)} report field(s):**")
            lines += [f"- {b}" for b in d.broken_visuals]
            lines.append("")
        if d.broken_references:
            lines.append(f"**Breaks {len(d.broken_references)} reference(s):**")
            lines += [f"- {f.message}" for f in d.broken_references]
            lines.append("")
        if not d.changes:
            lines += ["No model changes.", ""]
        for c in d.changes:
            if c.change != "changed":
                lines.append(f"- {c.change.capitalize()} {c.kind} `{c.object_path}`")
                continue
            parts = "; ".join(f"{fname}: {_short(o)} -> {_short(n)}" for fname, o, n in c.fields)
            lines.append(f"- Changed {c.kind} `{c.object_path}`: {parts}")
        if d.new_findings or d.resolved_findings:
            lines.append("")
            lines.append(
                f"Findings: {len(d.resolved_findings)} resolved, {len(d.new_findings)} new."
            )
            lines += [f"- new {f.severity}: {f.message}" for f in d.new_findings
                      if f.rule != "unknown-reference"]  # fmt: skip
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
