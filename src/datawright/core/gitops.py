"""Git plumbing. Commits are built in a temporary index, so the user's working tree, index and
current branch are never touched."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Datawright",
    "GIT_AUTHOR_EMAIL": "datawright@localhost",
    "GIT_COMMITTER_NAME": "Datawright",
    "GIT_COMMITTER_EMAIL": "datawright@localhost",
}
ZERO_SHA = "0" * 40


class GitError(RuntimeError):
    pass


def git(
    repo: Path,
    *args: str,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> bytes:
    full_env = {**os.environ, **IDENTITY, **(env or {})}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, capture_output=True, env=full_env
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:2])} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def out(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git(repo, *args, env=env).decode().strip()


def is_repo(path: Path) -> bool:
    try:
        return out(path, "rev-parse", "--is-inside-work-tree") == "true"
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def toplevel(path: Path) -> Path:
    return Path(out(path, "rev-parse", "--show-toplevel"))


def head_sha(repo: Path) -> str:
    return out(repo, "rev-parse", "HEAD")


def current_branch(repo: Path) -> str:
    return out(repo, "rev-parse", "--abbrev-ref", "HEAD")


def branch_sha(repo: Path, branch: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    sha = proc.decode().strip()
    return sha or None


def blob_sha(repo: Path, rev: str, path: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}:{path}", check=False)
    sha = proc.decode().strip()
    return sha or None


def show(repo: Path, rev: str, path: str) -> bytes:
    return git(repo, "show", f"{rev}:{path}")


def hash_content(repo: Path, path: str, content: bytes, write: bool = False) -> str:
    """Blob id Git would store for `content` at `path` (applies the repo's line-ending filters)."""
    args = ["hash-object", f"--path={path}", "--stdin"]
    if write:
        args.insert(1, "-w")
    return git(repo, *args, stdin=content).decode().strip()


def _mode(repo: Path, rev: str, path: str) -> str:
    line = out(repo, "ls-tree", rev, "--", path)
    return line.split()[0] if line else "100644"


def commit_files(repo: Path, parent: str, files: dict[str, bytes], message: str) -> str:
    """Create a commit = `parent` + `files`, without touching the working tree or real index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", parent, env=env)
        for path, content in files.items():
            blob = hash_content(repo, path, content, write=True)
            git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"{_mode(repo, parent, path)},{blob},{path}",
                env=env,
            )
        tree = out(repo, "write-tree", env=env)
    return out(repo, "commit-tree", tree, "-p", parent, "-m", message)


def update_branch(repo: Path, branch: str, new_sha: str, expected_old: str | None) -> None:
    """Move refs/heads/<branch>; `expected_old=None` means the branch must not exist yet."""
    git(repo, "update-ref", f"refs/heads/{branch}", new_sha, expected_old or ZERO_SHA)
