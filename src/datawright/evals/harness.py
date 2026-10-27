"""Eval sets in evals/datasets/<name>.yaml; pass-rate thresholds in evals/thresholds.yaml.

Kinds:
- audit-rules: deterministic; each case lists the exact findings one rule must produce.
- describe:    runs the documenter on a fixture and checks each proposed description.
- ask:         runs the ask agent and checks that the answer cites the expected objects.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.runtime import MessagesClient
from datawright.config import EVALS_DIR, FIXTURES_DIR, REPO_ROOT, Settings
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.rules import run_rules
from datawright.core import gitops
from datawright.db import migrate, session_factory
from datawright.fixtures import make_fixture_repo
from datawright.models import ChangeSet, Connection, EvalResult, User

RAW_DAX = re.compile(r"\b[A-Z][A-Z0-9.]{2,}\s*\(")
ClientFactory = Callable[[Any], tuple[MessagesClient, str]]  # offline script -> (client, label)


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
    keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
    if any(os.environ.get(k) for k in keys):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def _audit_rules(spec: dict[str, Any]) -> list[CaseResult]:
    found_by_fixture: dict[str, set[tuple[str, str]]] = {}
    out = []
    for case in spec["cases"]:
        fixture = case["fixture"]
        if fixture not in found_by_fixture:
            models = open_source(FIXTURES_DIR / "pbip" / fixture).models().values()
            found_by_fixture[fixture] = {(f.rule, f.object_path) for f in run_rules(models)}
        found = found_by_fixture[fixture]
        if "rule" in case:
            found = {f for f in found if f[0] == case["rule"]}
            expected = {(case["rule"], o) for o in case.get("objects") or []}
        else:
            expected = set()
        missing, extra = sorted(expected - found), sorted(found - expected)
        detail = "; ".join(
            [f"missing {m[1]}" for m in missing] + [f"unexpected {x}" for x in extra]
        )
        out.append(CaseResult(case["id"], not missing and not extra, detail))
    return out


def _diagnose(spec: dict[str, Any]) -> list[CaseResult]:
    from datawright.connectors.powerbi.refresh import diagnose

    out = []
    for case in spec["cases"]:
        models = list(open_source(FIXTURES_DIR / "pbip" / case["fixture"]).models().values())
        found = diagnose(case["error"], models)
        match = next((d for d in found if d.pattern == case["pattern"]), None)
        if match is None:
            got = [d.pattern for d in found]
            out.append(CaseResult(case["id"], False, f"expected {case['pattern']}, got {got}"))
            continue
        paths = {e.object_path for e in match.evidence}
        missing = [o for o in case.get("evidence") or [] if o not in paths]
        detail = f"missing evidence {missing}" if missing else f"{len(paths)} objects"
        out.append(CaseResult(case["id"], not missing, detail))
    return out


@contextmanager
def _fixture_workspace(
    fixture: str, settings: Settings
) -> Iterator[tuple[Session, User, Connection, Settings]]:
    """A throwaway Git repo, database and connection for one model-based eval run."""
    from datawright.services import audit, bootstrap, connect

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo = make_fixture_repo(fixture, tmp_path / "repo")
        run_settings = settings.model_copy(update={"data_dir": tmp_path / "data"})
        run_settings.data_dir.mkdir()
        migrate(run_settings.db_url)
        sessions = session_factory(run_settings.db_url)
        try:
            with sessions() as s:
                user = bootstrap(s)
                conn = connect(s, user, "eval", repo, "propose")
                audit(s, conn)
                yield s, user, conn, run_settings
        finally:
            sessions.kw["bind"].dispose()  # release the file so Windows can delete the folder


def _describe(
    spec: dict[str, Any], settings: Settings, factory: ClientFactory
) -> tuple[list[CaseResult], str]:
    from datawright.agents.documenter import offline_script, run_documenter

    cases = spec["cases"]
    limit = int(spec.get("limit", len(cases)))
    client, label = factory(offline_script(limit))
    with _fixture_workspace(spec["fixture"], settings) as (s, user, conn, run_settings):
        run = run_documenter(
            s,
            run_settings,
            client,
            conn,
            user,
            limit=limit,
            kinds=spec.get("kinds", ["measure", "column", "table"]),
            model_label=label,
        )
        cs = s.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))
        proposals = {
            i["object_path"]: i["value"] for item in (cs.items if cs else []) for i in item.intents
        }
        run_error = run.error
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
    return out, label


def _ask(
    spec: dict[str, Any], settings: Settings, factory: ClientFactory
) -> tuple[list[CaseResult], str]:
    from datawright.agents.ask import offline_script, run_ask

    client, label = factory(offline_script)
    out = []
    with _fixture_workspace(spec["fixture"], settings) as (s, user, conn, run_settings):
        for case in spec["cases"]:
            run = run_ask(s, run_settings, client, conn, user, case["question"], model_label=label)
            answer = run.final_text or ""
            cited = [o for o in case["must_cite_any"] if o in answer]
            ok = run.status == "succeeded" and bool(cited)
            detail = (
                "cited " + ", ".join(cited)
                if cited
                else f"cited none of {case['must_cite_any']} ({run.status}: {answer[:120]!r})"
            )
            out.append(CaseResult(case["id"], ok, detail))
    return out, label


def run_set(
    session: Session,
    settings: Settings,
    name: str,
    client_factory: ClientFactory | None,
    offline: bool = False,
) -> SetResult:
    spec = load_set(name)
    kind = spec["kind"]
    if kind == "audit-rules":
        result = SetResult(name, "rules", threshold_for(name), _audit_rules(spec))
    elif kind == "diagnose":
        result = SetResult(name, "patterns", threshold_for(name), _diagnose(spec))
    elif kind in ("describe", "ask"):
        if client_factory is None:
            return SetResult(
                name,
                settings.model,
                threshold_for(name),
                skipped="needs Claude credentials (or run with --offline)",
            )
        runner = _describe if kind == "describe" else _ask
        cases, label = runner(spec, settings, client_factory)
        result = SetResult(name, label, threshold_for(name), cases)
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
