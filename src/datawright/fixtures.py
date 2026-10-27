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
