"""Which model fields a report's visuals and filters use.

Reads PBIR (`<name>.Report/definition/pages/*/visuals/*/visual.json`), the older single-file
PBIP layout (`<name>.Report/report.json`) and the layout inside a `.pbix` (`Report/Layout`).
All three describe fields the same way: an expression with a SourceRef to an entity (table),
plus a Property (column or measure name). Older layouts use aliases (`From` lists) and embed
visual configs as JSON strings; the walker handles both.
"""

from __future__ import annotations

import json
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FieldKey = tuple[str, str]  # (table, column or measure), casefolded


@dataclass
class ReportUsage:
    """Fields used by one or more reports, with where they're used (for messages)."""

    reports: list[str] = field(default_factory=list)
    visuals: int = 0  # a blank report says nothing about usage
    uses: dict[FieldKey, list[str]] = field(default_factory=lambda: defaultdict(list))

    def used(self, table: str, name: str) -> bool:
        return (table.casefold(), name.casefold()) in self.uses

    def where(self, table: str, name: str) -> list[str]:
        return self.uses.get((table.casefold(), name.casefold()), [])

    def add(self, table: str, name: str, where: str) -> None:
        places = self.uses[(table.casefold(), name.casefold())]
        if where not in places:
            places.append(where)

    def merge(self, other: ReportUsage) -> None:
        self.reports += other.reports
        self.visuals += other.visuals
        for key, places in other.uses.items():
            for p in places:
                if p not in self.uses[key]:
                    self.uses[key].append(p)


def field_refs(node: Any, aliases: dict[str, str] | None = None) -> Iterator[tuple[str, str]]:
    """(table, field) for every field reference in a layout JSON tree."""
    aliases = dict(aliases or {})
    if isinstance(node, str):
        text = node.strip()
        if text[:1] in "{[" and ("SourceRef" in text or "From" in text):
            try:
                yield from field_refs(json.loads(text), aliases)
            except ValueError:
                return
        return
    if isinstance(node, list):
        for item in node:
            yield from field_refs(item, aliases)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("From"), list):
        for f in node["From"]:
            if isinstance(f, dict) and f.get("Name") and f.get("Entity"):
                aliases[str(f["Name"])] = str(f["Entity"])
    expr, prop = node.get("Expression"), node.get("Property")
    if isinstance(expr, dict) and isinstance(prop, str) and isinstance(expr.get("SourceRef"), dict):
        src = expr["SourceRef"]
        entity = src.get("Entity") or aliases.get(str(src.get("Source")))
        if entity:
            yield str(entity), prop
    for value in node.values():
        yield from field_refs(value, aliases)


def _read_json(path: Path) -> Any:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _legacy_layout(layout: Any, report: str) -> ReportUsage:
    usage = ReportUsage([report])
    for section in (layout or {}).get("sections", []):
        page = section.get("displayName") or section.get("name") or "page"
        for container in section.get("visualContainers", []):
            usage.visuals += 1
            config = container.get("config")
            try:
                cfg = json.loads(config) if isinstance(config, str) else (config or {})
            except ValueError:
                cfg = {}
            vtype = (cfg.get("singleVisual") or {}).get("visualType", "visual")
            for table, name in field_refs(container):
                usage.add(table, name, f"{report} / {page} / {vtype}")
        for table, name in field_refs(section.get("filters")):
            usage.add(table, name, f"{report} / {page} / page filter")
    for table, name in field_refs((layout or {}).get("filters")):
        usage.add(table, name, f"{report} / report filter")
    return usage


def _pbir(definition: Path, report: str) -> ReportUsage:
    usage = ReportUsage([report])
    for page_dir in sorted((definition / "pages").glob("*")):
        if not page_dir.is_dir():
            continue
        page_json = _read_json(page_dir / "page.json") if (page_dir / "page.json").exists() else {}
        page = (page_json or {}).get("displayName") or page_dir.name
        for table, name in field_refs(page_json):
            usage.add(table, name, f"{report} / {page} / page filter")
        for visual in sorted(page_dir.glob("visuals/*/visual.json")):
            usage.visuals += 1
            data = _read_json(visual) or {}
            vtype = (data.get("visual") or {}).get("visualType", "visual")
            for table, name in field_refs(data):
                usage.add(table, name, f"{report} / {page} / {vtype}")
    report_json = definition / "report.json"
    if report_json.exists():
        for table, name in field_refs(_read_json(report_json)):
            usage.add(table, name, f"{report} / report filter")
    return usage


def _target_model(report_dir: Path) -> str | None:
    """The semantic model a PBIP report points at (`byPath`), if it's local."""
    pbir = report_dir / "definition.pbir"
    data = _read_json(pbir) if pbir.exists() else None
    path = (((data or {}).get("datasetReference") or {}).get("byPath") or {}).get("path")
    if not path:
        return None
    name = Path(str(path).replace("\\", "/")).name
    return name[: -len(".SemanticModel")] if name.endswith(".SemanticModel") else None


def pbip_report_usage(root: Path, model_names: list[str]) -> dict[str, ReportUsage]:
    """Report usage per model for every `*.Report` folder under root."""
    out: dict[str, ReportUsage] = {}
    for report_dir in sorted(root.rglob("*.Report")):
        if ".git" in report_dir.parts or not report_dir.is_dir():
            continue
        report = report_dir.name[: -len(".Report")]
        if (report_dir / "definition" / "pages").is_dir():
            usage = _pbir(report_dir / "definition", report)
        elif (report_dir / "report.json").exists():
            usage = _legacy_layout(_read_json(report_dir / "report.json"), report)
        else:
            continue
        target = _target_model(report_dir)
        if target is None and len(model_names) == 1:
            target = model_names[0]
        if target in model_names:
            out.setdefault(target, ReportUsage()).merge(usage)
    return {name: u for name, u in out.items() if u.visuals}


def pbix_report_usage(path: Path) -> ReportUsage | None:
    """Report usage from inside a .pbix: PBIR (`Report/definition/...`, newer files) or the
    classic `Report/Layout`. None if the file has no report."""
    import tempfile

    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            pbir = [n for n in names if n.startswith("Report/definition/") and n.endswith(".json")]
            if pbir:
                with tempfile.TemporaryDirectory() as tmp:
                    for n in pbir:
                        target = Path(tmp) / n
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(z.read(n))
                    usage = _pbir(Path(tmp) / "Report" / "definition", path.stem)
                    return usage if usage.visuals else None
            if "Report/Layout" not in names:
                return None
            raw = z.read("Report/Layout")
    except (OSError, zipfile.BadZipFile):
        return None
    try:
        layout = json.loads(raw.decode("utf-16-le").lstrip(chr(0xFEFF)))
    except (UnicodeDecodeError, ValueError):
        return None
    usage = _legacy_layout(layout, path.stem)
    return usage if usage.visuals else None
