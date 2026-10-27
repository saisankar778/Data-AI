"""Eval sets in evals/datasets/<name>.yaml; pass-rate thresholds in evals/thresholds.yaml."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.runtime import MessagesClient
from datawright.config import EVALS_DIR, FIXTURES_DIR, REPO_ROOT, Settings
from datawright.connectors.powerbi.pbip import PbipRepoConnector
from datawright.connectors.powerbi.rules import run_rules
from datawright.core import gitops
from datawright.db import migrate, session_factory
from datawright.fixtures import make_fixture_repo
from datawright.models import ChangeSet, EvalResult

RAW_DAX = re.compile(r"\b[A-Z][A-Z0-9.]{2,}\s*\(")


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    detail: str = ""


@dataclass
class SetResult:
    name: str
    model: str
    threshold: float
    cases: list[CaseResult] = field(default_factory=list)
    skipped: str | None = None

    @property
    def pass_rate(self) -> float:
        return sum(c.passed for c in self.cases) / len(self.cases) if self.cases else 0.0

    @property
    def ok(self) -> bool:
        return self.skipped is not None or self.pass_rate >= self.threshold


def load_set(name: str) -> dict[str, Any]:
    path = EVALS_DIR / "datasets" / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no eval set {name!r} ({path})")
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def threshold_for(name: str) -> float:
    data = yaml.safe_load((EVALS_DIR / "thresholds.yaml").read_text(encoding="utf-8")) or {}
    return float(data.get(name, 1.0))


def has_credentials() -> bool:
    if any(
        os.environ.get(k)
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
    ):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def _audit_rules(spec: dict[str, Any]) -> list[CaseResult]:
    out = []
    for case in spec["cases"]:
        connector = PbipRepoConnector(FIXTURES_DIR / "pbip" / case["fixture"])
        found = {(f.rule, f.object_path) for f in run_rules(connector.models().values())}
        expected = {(e["rule"], e["object"]) for e in case.get("expect") or []}
        missing, extra = sorted(expected - found), sorted(found - expected)
        detail = "; ".join([f"missing {m}" for m in missing] + [f"unexpected {x}" for x in extra])
        out.append(CaseResult(case["id"], not missing and not extra, detail))
    return out


def _describe(
    spec: dict[str, Any], settings: Settings, client: MessagesClient, model_label: str
) -> list[CaseResult]:
    # Imported here: the documenter pulls in the agent stack, which the rule evals don't need.
    from datawright.agents.documenter import run_documenter
    from datawright.services import audit, bootstrap, connect

    cases = spec["cases"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = make_fixture_repo(spec["fixture"], tmp_path / "repo")
        run_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
        run_settings.data_dir.mkdir()
        migrate(run_settings.db_url)
        sessions = session_factory(run_settings.db_url)
        try:
            with sessions() as s:
                user = bootstrap(s)
                conn = connect(s, user, "eval", repo, "propose")
                audit(s, conn)
                run = run_documenter(
                    s, run_settings, client, conn, user, limit=len(cases), model_label=model_label
                )
                cs = s.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))
                proposals = {
                    i["object_path"]: i["value"]
                    for item in (cs.items if cs else [])
                    for i in item.intents
                }
                run_error = run.error
        finally:
            # Release the SQLite file so the temp folder can be deleted on Windows.
            sessions.kw["bind"].dispose()
    out = []
    for case in cases:
        desc = proposals.get(case["object"])
        problems = []
        if not desc:
            problems.append(f"no description proposed{f' (run: {run_error})' if run_error else ''}")
        else:
            if len(desc) > 300:
                problems.append(f"{len(desc)} chars > 300")
            if RAW_DAX.search(desc):
                problems.append("contains raw DAX")
            mentions = case.get("must_mention_any") or []
            if mentions and not any(m.lower() in desc.lower() for m in mentions):
                problems.append(f"mentions none of {mentions}")
        out.append(CaseResult(case["id"], not problems, "; ".join(problems) or desc or ""))
    return out


def run_set(
    session: Session,
    settings: Settings,
    name: str,
    client_factory: Callable[[], tuple[MessagesClient, str]] | None,
    offline: bool = False,
) -> SetResult:
    spec = load_set(name)
    kind = spec["kind"]
    if kind == "audit-rules":
        result = SetResult(name, "rules", threshold_for(name), _audit_rules(spec))
    elif kind == "describe":
        if client_factory is None:
            return SetResult(
                name,
                settings.model,
                threshold_for(name),
                skipped="needs Claude credentials (or run with --offline)",
            )
        client, label = client_factory()
        result = SetResult(
            name, label, threshold_for(name), _describe(spec, settings, client, label)
        )
    else:
        raise ValueError(f"unknown eval kind {kind!r}")
    sha = None
    if gitops.is_repo(REPO_ROOT):
        try:
            sha = gitops.head_sha(REPO_ROOT)
        except gitops.GitError:
            sha = None  # repository has no commits yet
    for c in result.cases:
        session.add(
            EvalResult(
                eval_set=name,
                case_id=c.case_id,
                passed=c.passed,
                score=1.0 if c.passed else 0.0,
                detail=c.detail,
                model=result.model,
                git_sha=sha,
            )
        )
    session.commit()
    return result
