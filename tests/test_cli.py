from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from datawright.cli import app
from datawright.evals import harness

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(tmp_path / "data"))


def test_offline_demo(tmp_path: Path) -> None:
    r = runner.invoke(app, ["demo", "--offline"])
    assert r.exit_code == 0, r.output
    assert "Audit: 13 findings." in r.output
    assert "Change set #1: proposed, 3 edits" in r.output


def test_audit_rules_eval_passes() -> None:
    r = runner.invoke(app, ["evals", "run", "--set", "audit-rules-v0"])
    assert r.exit_code == 0, r.output
    assert "100% passed" in r.output


def test_model_eval_is_skipped_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "has_credentials", lambda: False)
    r = runner.invoke(app, ["evals", "run", "--set", "describe-v0"])
    assert r.exit_code == 0 and "skipped" in r.output


def test_model_eval_runs_offline() -> None:
    r = runner.invoke(app, ["evals", "run", "--set", "describe-v0", "--offline"])
    assert "describe-v0 [scripted-offline]" in r.output, r.output


def test_document_without_credentials_explains(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(harness, "has_credentials", lambda: False)
    assert runner.invoke(app, ["connect", "r", str(repo)]).exit_code == 0
    r = runner.invoke(app, ["document", "r"])
    assert r.exit_code == 2 and "--offline" in r.output
