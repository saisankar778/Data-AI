from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from datawright.cli import app
from datawright.config import FIXTURES_DIR
from datawright.evals import harness

runner = CliRunner()
PBIX = FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix"


@pytest.fixture(autouse=True)
def isolated_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAWRIGHT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)  # no stray .env from the developer's folder


def test_offline_demo() -> None:
    r = runner.invoke(app, ["demo", "--offline"])
    assert r.exit_code == 0, r.output
    assert "Audit: 62 findings." in r.output
    assert "Change set #1: proposed, 5 edits" in r.output


def test_check_folder_and_pbix(tmp_path: Path) -> None:
    r = runner.invoke(app, ["check", str(FIXTURES_DIR / "pbip" / "retail-issues")])
    assert r.exit_code == 0, r.output
    assert "Retail (pbip): health 0/100" in r.output and "Top problems:" in r.output
    r = runner.invoke(app, ["check", str(PBIX), "--all"])
    assert r.exit_code == 0, r.output
    assert "Revenue Opportunities (pbix)" in r.output and "AI-readiness" in r.output
    (tmp_path / "empty").mkdir()
    r = runner.invoke(app, ["check", str(tmp_path / "empty")])
    assert r.exit_code == 1 and "no Power BI model" in r.output


def test_pbix_connection_is_read_only() -> None:
    r = runner.invoke(app, ["connect", "rev", str(PBIX)])
    assert r.exit_code == 0, r.output
    assert "pbix_file, read" in r.output
    r = runner.invoke(app, ["connect", "rev2", str(PBIX), "--capability", "apply"])
    assert r.exit_code == 1 and "Save as" in r.output
    r = runner.invoke(app, ["guide", "rev"])
    assert r.exit_code == 0 and "# Model guide: Revenue Opportunities" in r.output


def test_track_then_connect(tmp_path: Path) -> None:
    import shutil

    folder = tmp_path / "project"
    shutil.copytree(FIXTURES_DIR / "pbip" / "retail-clean", folder)
    assert runner.invoke(app, ["connect", "p", str(folder)]).exit_code == 1  # not in Git yet
    r = runner.invoke(app, ["track", str(folder), "--yes"])
    assert r.exit_code == 0 and "Now tracking" in r.output, r.output
    assert ".pbi/cache.abf" in (folder / ".gitignore").read_text()
    r = runner.invoke(app, ["connect", "p", str(folder), "--capability", "apply"])
    assert r.exit_code == 0, r.output
    assert "already tracked" in runner.invoke(app, ["track", str(folder), "--yes"]).output


def test_ask_offline(repo: Path) -> None:
    assert runner.invoke(app, ["connect", "r", str(repo)]).exit_code == 0
    r = runner.invoke(app, ["ask", "r", "Which measure shows margin?", "--offline"])
    assert r.exit_code == 0, r.output
    assert "Retail/Sales/measure/Margin %" in r.output


def test_audit_rules_eval_passes() -> None:
    r = runner.invoke(app, ["evals", "run", "--set", "audit-rules-v1"])
    assert r.exit_code == 0, r.output
    assert "100% passed" in r.output


def test_model_evals_skip_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "has_credentials", lambda: False)
    for name in ("describe-v1", "ask-v0"):
        r = runner.invoke(app, ["evals", "run", "--set", name])
        assert r.exit_code == 0 and "skipped" in r.output


def test_model_evals_run_offline() -> None:
    r = runner.invoke(app, ["evals", "run", "--set", "describe-v1", "--offline"])
    assert "describe-v1 [scripted-offline]" in r.output, r.output
    r = runner.invoke(app, ["evals", "run", "--set", "ask-v0", "--offline"])
    assert "ask-v0 [scripted-offline]" in r.output, r.output


def test_document_without_credentials_explains(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(harness, "has_credentials", lambda: False)
    assert runner.invoke(app, ["connect", "r", str(repo)]).exit_code == 0
    r = runner.invoke(app, ["document", "r"])
    assert r.exit_code == 2 and "--offline" in r.output and ".env" in r.output
