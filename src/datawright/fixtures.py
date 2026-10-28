"""Turn a fixture folder into a fresh Git repository (used by the demo, evals and tests)."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from datawright.config import FIXTURES_DIR
from datawright.core import gitops


def force_rmtree(path: Path) -> None:
    def make_writable_and_retry(func: Any, p: str, _exc: Any) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=make_writable_and_retry)


SAMPLES = ("retail-clean", "retail-issues", "retail-report")
PARAMETER_META = 'meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]'


def make_sample(name: str, dest: Path) -> Path:
    """Copy a sample Power BI project to `dest` with its CSV data in `dest/data`, and point the
    model at that data, so it opens and refreshes in Power BI Desktop. Never touches fixtures."""
    from datawright.connectors.powerbi import tmdl

    if name not in SAMPLES:
        raise ValueError(f"unknown sample {name!r}; choose one of {', '.join(SAMPLES)}")
    dest = dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"{dest} already exists and isn't empty; choose a new folder")
    shutil.copytree(FIXTURES_DIR / "pbip" / name, dest, dirs_exist_ok=True)
    data = dest / "data"
    shutil.copytree(FIXTURES_DIR / "data", data)
    folder = str(data) + os.sep
    for model_dir in dest.glob("*.SemanticModel/definition"):
        expressions = model_dir / "expressions.tmdl"
        if expressions.exists():  # retail-clean/report: a DataFolder parameter
            text = tmdl.read_text(expressions)
            value = f'"{folder}" {PARAMETER_META}'
            text = tmdl.set_expression(text, "expression", "DataFolder", value)
            expressions.write_bytes(text.encode("utf-8"))
        for table_file in (model_dir / "tables").glob("*.tmdl"):
            text = tmdl.read_text(table_file)
            if '"C:' + "\\Data\\" in text:  # retail-issues: hard-coded paths (kept hard-coded)
                text = text.replace('"C:' + "\\Data\\", '"' + folder)
                table_file.write_bytes(text.encode("utf-8"))
    return dest


def make_fixture_repo(fixture: str, dest: Path) -> Path:
    src = FIXTURES_DIR / "pbip" / fixture
    if not src.is_dir():
        raise FileNotFoundError(f"fixture {fixture!r} not found in {FIXTURES_DIR / 'pbip'}")
    if dest.exists():
        force_rmtree(dest)
    shutil.copytree(src, dest)
    gitops.git(dest, "init", "-q", "-b", "main")
    gitops.git(dest, "config", "core.autocrlf", "false")
    gitops.git(dest, "add", "-A")
    gitops.git(dest, "commit", "-q", "-m", f"Import fixture {fixture}")
    return dest
