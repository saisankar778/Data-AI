"""Machine-readable health-check output for CI: JSON, SARIF 2.1.0 and GitHub annotations.

Findings on PBIP models carry the TMDL file and line of the object, so code-scanning alerts and
annotations land on the exact declaration in a pull request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from datawright.connectors.powerbi.fixes import guidance
from datawright.connectors.powerbi.health import HealthReport
from datawright.connectors.powerbi.pbip import PowerBIConnector
from datawright.connectors.powerbi.rules import RULES, RuleFinding, rule_info
from datawright.connectors.powerbi.tmdl import TmdlDocument, parse, read_text
from datawright.core.paths import split_path

SARIF_LEVEL = {"error": "error", "warning": "warning", "info": "note"}
SEVERITY_RANK = {"info": 1, "warning": 2, "error": 3}


def _find_line(doc: TmdlDocument, kind: str, name: str, parent: tuple[str, str] | None) -> int:
    obj = doc.find(kind, name, parent)
    return obj.line + 1 if obj is not None else 1


def locate(connector: PowerBIConnector, object_path: str) -> tuple[Path, int] | None:
    """(absolute TMDL file, 1-based line) of the object a finding is about; None for .pbix."""
    parts = split_path(object_path)
    model = connector.models().get(parts[0]) if parts else None
    if model is None or model.definition_dir is None:
        return None
    base = model.definition_dir
    try:
        if len(parts) == 1:
            path = base / "model.tmdl"
            return (path, 1) if path.exists() else None
        if parts[1] == "relationship" and len(parts) == 3:
            path = base / "relationships.tmdl"
            return path, _find_line(parse(read_text(path)), "relationship", parts[2], None)
        if parts[1] == "expression" and len(parts) == 3:
            path = base / "expressions.tmdl"
            return path, _find_line(parse(read_text(path)), "expression", parts[2], None)
        table = model.tables.get(parts[1])
        if table is None or table.file is None:
            return None
        path = base / table.file
        doc = parse(read_text(path))
        if len(parts) == 2:
            return path, _find_line(doc, "table", table.name, None)
        return path, _find_line(doc, parts[2], parts[3], ("table", table.name))
    except (OSError, ValueError):
        return None


def _rel(path: Path) -> str:
    try:
        return Path(os.path.relpath(path, Path.cwd())).as_posix()
    except ValueError:  # another drive on Windows
        return path.as_posix()


def located(connector: PowerBIConnector, reports: list[HealthReport]) -> list[dict[str, Any]]:
    rows = []
    for h in reports:
        for f in h.findings:
            where = locate(connector, f.object_path)
            rows.append({
                "model": h.model, "rule": f.rule, "severity": f.severity, "category": f.category,
                "object_path": f.object_path, "message": f.message,
                "file": _rel(where[0]) if where else None, "line": where[1] if where else None,
            })  # fmt: skip
    return rows


def to_json(connector: PowerBIConnector, reports: list[HealthReport]) -> str:
    findings = located(connector, reports)
    return json.dumps({
        "models": [
            {"model": h.model, "source": h.source, "score": h.score, "grade": h.grade,
             "ai_readiness_percent": h.ai_readiness, "tables": h.tables, "columns": h.columns,
             "measures": h.measures, "findings_by_severity": dict(h.by_severity)}
            for h in reports
        ],
        "findings": findings,
    }, indent=2, ensure_ascii=False)  # fmt: skip


def to_sarif(connector: PowerBIConnector, reports: list[HealthReport]) -> str:
    used = {f.rule for h in reports for f in h.findings}
    rules = [
        {
            "id": info.id, "name": info.title,
            "shortDescription": {"text": info.title},
            "fullDescription": {"text": info.why},
            "help": {"text": guidance(info.id) or info.why},
            "defaultConfiguration": {"level": SARIF_LEVEL[info.severity]},
            "properties": {"category": info.category, **({"bpa": info.bpa} if info.bpa else {})},
        }
        for info, _ in RULES if info.id in used
    ]  # fmt: skip
    results = []
    for row in located(connector, reports):
        result: dict[str, Any] = {
            "ruleId": row["rule"], "level": SARIF_LEVEL[row["severity"]],
            "message": {"text": f"{row['message']} ({row['object_path']})"},
            "partialFingerprints": {"objectPath": row["object_path"]},
        }  # fmt: skip
        if row["file"]:
            result["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": row["file"]}, "region": {"startLine": row["line"]},
            }}]  # fmt: skip
        results.append(result)
    return json.dumps({
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {"name": "Datawright",
                                "informationUri": "https://github.com/saisankar778/Data-AI",
                                "rules": rules}},
            "results": results,
        }],
    }, indent=2, ensure_ascii=False)  # fmt: skip


def _gh_escape(text: str, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def to_github(connector: PowerBIConnector, reports: list[HealthReport]) -> str:
    """GitHub Actions workflow commands: annotations on the PR's files."""
    level = {"error": "error", "warning": "warning", "info": "notice"}
    lines = []
    for row in located(connector, reports):
        info = rule_info(row["rule"])
        props = [f"title={_gh_escape(info.title if info else row['rule'], True)}"]
        if row["file"]:
            props.insert(0, f"file={_gh_escape(row['file'], True)},line={row['line']}")
        lines.append(f"::{level[row['severity']]} {','.join(props)}::"
                     f"{_gh_escape(row['message'])}")  # fmt: skip
    for h in reports:
        lines.append(f"{h.model}: health {h.score}/100 ({h.grade}), AI-ready {h.ai_readiness}%, "
                     f"{len(h.findings)} findings")  # fmt: skip
    return "\n".join(lines)


def gate_failures(reports: list[HealthReport], fail_on: str, min_score: int | None) -> list[str]:
    """Reasons the CI gate fails (empty list = pass)."""
    reasons = []
    if fail_on != "none":
        threshold = SEVERITY_RANK[fail_on]
        bad: list[RuleFinding] = [f for h in reports for f in h.findings
                                  if SEVERITY_RANK[f.severity] >= threshold]  # fmt: skip
        if bad:
            reasons.append(f"{len(bad)} finding(s) at or above '{fail_on}'")
    if min_score is not None:
        for h in reports:
            if h.score < min_score:
                reasons.append(f"{h.model} scores {h.score}, below the minimum {min_score}")
    return reasons
