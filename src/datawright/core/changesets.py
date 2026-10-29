"""Change sets: the only path to a write (docs/specs/core.md, "Change set lifecycle")."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.connectors.edits import CREATES_FILES, apply_intent, check_item, replay
from datawright.core import audit_log, gitops
from datawright.core.capabilities import APPROVER_ROLES, Capability
from datawright.models import ChangeItem, ChangeSet, Connection, Finding, User, utcnow


class ChangeSetError(Exception):
    pass


class ConflictError(ChangeSetError):
    pass


def unified_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(ln if ln.endswith("\n") else ln + "\n" for ln in lines)


Decision = dict[str, Any]


def _fb(path: str, proposed: Any, final: Any, decision: str) -> dict[str, Any]:
    return {"object_path": path, "proposed": proposed, "final": final, "decision": decision}


def _normalise_value(intent: dict[str, Any]) -> Any:
    """How a reviewer's edited text is cleaned: one line for descriptions and DAX, but AI
    instructions are Markdown, so they keep their lines (browsers also send CRLF)."""
    if intent.get("op") == "copilot.set_instructions":
        return lambda s: "\n".join(
            ln.rstrip() for ln in s.replace("\r\n", "\n").split("\n")
        ).strip()
    return lambda s: " ".join(s.split())


def branch_name(cs: ChangeSet) -> str:
    return f"datawright/cs-{cs.id}"


class ChangeSetService:
    def __init__(self, session: Session, actor: User) -> None:
        self.session = session
        self.actor = actor

    # ------------------------------------------------------------ drafting

    def create_draft(
        self, connection: Connection, title: str, run_id: int | None = None
    ) -> ChangeSet:
        cs = ChangeSet(connection_id=connection.id, run_id=run_id, title=title, status="draft")
        self.session.add(cs)
        self.session.flush()
        audit_log.record(
            self.session, self.actor.name, "change_set.create", "change_set", cs.id, run_id=run_id
        )
        return cs

    def add_edit(self, cs: ChangeSet, file_path: str, intent: dict[str, Any]) -> ChangeItem:
        """Add one edit. Edits to the same file accumulate in one item."""
        self._require(cs, "draft")
        item = next((i for i in cs.items if i.file_path == file_path), None)
        if item is None:
            path = Path(cs.connection.path) / file_path
            if path.exists() or intent.get("op") not in CREATES_FILES:
                before = path.read_bytes().decode("utf-8")
            else:
                before = ""  # a new file (Copilot settings)
            item = ChangeItem(
                change_set_id=cs.id,
                file_path=file_path,
                intents=[],
                before=before,
                after=before,
                diff="",
            )
            self.session.add(item)
            cs.items.append(item)
        new_after = apply_intent(item.after, intent)
        next_id = 1 + max((int(i.get("id", 0)) for it in cs.items for i in it.intents), default=0)
        item.intents = [*item.intents, {**intent, "id": str(next_id)}]
        item.after = new_after
        item.diff = unified_diff(file_path, item.before, item.after)
        self.session.flush()
        return item

    def propose(self, cs: ChangeSet) -> list[str]:
        """Validate every item; move to `proposed` only when there are no problems."""
        self._require(cs, "draft")
        problems = []
        if not cs.items:
            problems.append("change set has no items")
        for item in cs.items:
            problems += [
                f"{item.file_path}: {p}" for p in check_item(item.before, item.after, item.intents)
            ]
        if not problems:
            self._move(cs, "proposed")
        return problems

    # ------------------------------------------------------------ review

    def approve(
        self,
        cs: ChangeSet,
        comment: str | None = None,
        decisions: dict[str, Decision] | None = None,
    ) -> None:
        """Approve, optionally deciding edit by edit.

        `decisions` maps an object path to {"accept": bool, "value": str | None}. Paths not in it
        are accepted as proposed. Rejected edits are dropped, edited ones replace the proposed
        text, and the remaining edits are re-validated before the set is approved.
        """
        self._require(cs, "proposed")
        self._require_approver()
        decisions = decisions or {}
        feedback: list[dict[str, Any]] = []
        rejected_paths: set[str] = set()
        for item in list(cs.items):
            keep = []
            for intent in item.intents:
                path = str(intent.get("object_path", ""))
                proposed = intent.get("value")
                d = decisions.get(str(intent.get("id", ""))) or decisions.get(path, {})
                if d.get("accept", True) is False:
                    feedback.append(_fb(path, proposed, None, "rejected"))
                    rejected_paths.add(path)
                    continue
                final = (
                    d.get("value") if isinstance(proposed, str) else None
                )  # flags and lists can't be edited
                norm = _normalise_value(intent)
                if final is not None and norm(str(final)) != norm(str(proposed or "")):
                    keep.append(
                        {
                            **intent,
                            "value": norm(str(final)),
                            "proposed_value": proposed,
                        }
                    )
                    feedback.append(_fb(path, proposed, keep[-1]["value"], "edited"))
                else:
                    keep.append(intent)
                    feedback.append(_fb(path, proposed, proposed, "accepted"))
            try:
                item.after = replay(item.before, keep)
            except ValueError as e:
                raise ChangeSetError(f"{item.file_path}: {e}") from e
            item.intents = keep
            item.diff = unified_diff(item.file_path, item.before, item.after)
        if not any(i.intents for i in cs.items):
            raise ChangeSetError("every edit was rejected; reject the change set instead")
        problems = [
            f"{i.file_path}: {p}"
            for i in cs.items
            if i.intents
            for p in check_item(i.before, i.after, i.intents)
        ]
        if problems:
            raise ChangeSetError("; ".join(problems))
        for item in [i for i in cs.items if not i.intents]:
            self._unlink_findings([item.id], None)
            cs.items.remove(item)
            self.session.delete(item)
        self._unlink_findings([i.id for i in cs.items], rejected_paths)
        self._record_feedback(cs, feedback)
        cs.review_comment = (comment or "").strip() or None
        cs.reviewed_by = self.actor.name
        self._move(
            cs,
            "approved",
            comment=cs.review_comment,
            accepted=cs.accepted_count,
            edited=cs.edited_count,
            rejected=cs.rejected_count,
        )

    def reject(self, cs: ChangeSet, comment: str) -> None:
        self._require(cs, "proposed")
        self._require_approver()
        if not comment.strip():
            raise ChangeSetError("a rejection needs a comment (it becomes feedback for evals)")
        self._record_feedback(
            cs,
            [
                _fb(str(i.get("object_path", "")), i.get("value"), None, "rejected")
                for item in cs.items
                for i in item.intents
            ],
        )
        self._unlink_findings([i.id for i in cs.items], None)
        cs.review_comment = comment.strip()
        cs.reviewed_by = self.actor.name
        self._move(cs, "rejected", comment=cs.review_comment)

    def _record_feedback(self, cs: ChangeSet, feedback: list[dict[str, Any]]) -> None:
        cs.feedback = feedback
        cs.proposed_count = len(feedback)
        cs.accepted_count = sum(f["decision"] == "accepted" for f in feedback)
        cs.edited_count = sum(f["decision"] == "edited" for f in feedback)
        cs.rejected_count = sum(f["decision"] == "rejected" for f in feedback)

    def _unlink_findings(self, item_ids: list[int], paths: set[str] | None) -> None:
        """Findings tied to rejected edits go back to open, unlinked (paths=None: all)."""
        query = select(Finding).where(Finding.change_item_id.in_(item_ids))
        for f in self.session.scalars(query):
            if paths is None or f.object_path in paths:
                f.change_item_id = None
                f.status = "open"

    # ------------------------------------------------------------ writes

    def apply(self, cs: ChangeSet) -> str:
        self._require(cs, "approved")
        self._require_approver()
        conn = cs.connection
        if Capability.parse(conn.capability) < Capability.APPLY:
            raise ChangeSetError(
                f"connection {conn.name!r} allows '{conn.capability}', not 'apply'. "
                "An admin must raise it first."
            )
        repo = Path(conn.path)
        head = gitops.head_sha(repo)
        for item in cs.items:
            at_head = gitops.blob_sha(repo, head, item.file_path)
            if at_head is None and item.before == "":
                continue  # a file this change set creates, still absent
            expected = gitops.hash_content(repo, item.file_path, item.before.encode("utf-8"))
            if at_head != expected:
                raise ConflictError(
                    f"{item.file_path} at HEAD is not the version this change was made from "
                    "(it changed since, or has uncommitted edits). Commit your work and re-run."
                )
        branch = branch_name(cs)
        if gitops.branch_sha(repo, branch):
            raise ConflictError(f"branch {branch} already exists")
        parent, below = head, self._stack_base(cs, repo, head)
        if below is not None:
            parent = gitops.branch_sha(repo, below.branch or "") or head
            self._restack(cs, repo, parent, below)
        message = (
            f"Datawright change set #{cs.id}: {cs.title}\n\n"
            f"Approved by {cs.reviewed_by}. Agent run #{cs.run_id}."
        )
        if below is not None:
            message += (
                f"\nStacked on change set #{below.id} ({below.branch}): "
                "merging this branch brings in both."
            )
        commit = gitops.commit_files(
            repo, parent, {i.file_path: i.after.encode("utf-8") for i in cs.items}, message
        )
        gitops.update_branch(repo, branch, commit, None)
        cs.base_sha, cs.branch, cs.commit_sha, cs.error = parent, branch, commit, None
        self._set_findings(cs, "fixed")
        stacked = {"stacked_on": below.id} if below is not None else {}
        self._move(cs, "applied", branch=branch, commit=commit, **stacked)
        return commit

    def _stack_base(self, cs: ChangeSet, repo: Path, head: str) -> ChangeSet | None:
        """The newest applied set on this connection whose branch is unmerged work on HEAD."""
        query = (
            select(ChangeSet)
            .where(
                ChangeSet.connection_id == cs.connection_id,
                ChangeSet.status == "applied",
                ChangeSet.id != cs.id,
            )
            .order_by(ChangeSet.id.desc())
        )
        for other in self.session.scalars(query):
            tip = gitops.branch_sha(repo, other.branch or "")
            if tip and tip != head and gitops.is_ancestor(repo, head, tip):
                return other
        return None

    def _restack(self, cs: ChangeSet, repo: Path, tip: str, below: ChangeSet) -> None:
        """Replay the approved intents onto the files at `tip` and re-run the propose check."""
        for item in cs.items:
            if gitops.blob_sha(repo, tip, item.file_path) is None:
                if item.before == "":
                    continue  # still a new file on top of the stack
                raise ConflictError(f"{item.file_path} is missing on {below.branch}")
            base = gitops.show(repo, tip, item.file_path).decode("utf-8")
            if base == item.before:
                continue
            try:
                after = replay(base, item.intents)
            except ValueError as e:
                raise ConflictError(f"{item.file_path}: can't stack on #{below.id}: {e}") from e
            problems = check_item(base, after, item.intents)
            if problems:
                raise ConflictError(
                    f"{item.file_path}: can't stack on #{below.id}: {'; '.join(problems)}"
                )
            item.before, item.after = base, after
            item.diff = unified_diff(item.file_path, base, after)

    def rollback(self, cs: ChangeSet) -> str:
        self._require(cs, "applied")
        self._require_approver()
        repo = Path(cs.connection.path)
        assert cs.branch
        above = self.session.scalar(
            select(ChangeSet).where(
                ChangeSet.connection_id == cs.connection_id,
                ChangeSet.status == "applied",
                ChangeSet.base_sha == cs.commit_sha,
            )
        )
        if above is not None:
            raise ConflictError(
                f"change set #{above.id} is stacked on this one; roll back #{above.id} first"
            )
        tip = gitops.branch_sha(repo, cs.branch)
        if tip is None:
            raise ConflictError(f"branch {cs.branch} no longer exists")
        for item in cs.items:
            expected = gitops.hash_content(repo, item.file_path, item.after.encode("utf-8"))
            if gitops.blob_sha(repo, tip, item.file_path) != expected:
                raise ConflictError(f"{item.file_path} changed on {cs.branch} after the change set")
        restore: dict[str, bytes | None] = {}
        for i in cs.items:
            base = cs.base_sha or tip
            created = i.before == "" and gitops.blob_sha(repo, base, i.file_path) is None
            restore[i.file_path] = None if created else i.before.encode("utf-8")
        commit = gitops.commit_files(
            repo, tip, restore, f"Roll back Datawright change set #{cs.id}: {cs.title}"
        )
        gitops.update_branch(repo, cs.branch, commit, tip)
        cs.rollback_sha = commit
        self._set_findings(cs, "open")
        self._move(cs, "rolled_back", commit=commit)
        return commit

    # ------------------------------------------------------------ helpers

    def _require(self, cs: ChangeSet, status: str) -> None:
        if cs.status != status:
            raise ChangeSetError(f"change set #{cs.id} is '{cs.status}', expected '{status}'")

    def _require_approver(self) -> None:
        if self.actor.role not in APPROVER_ROLES:
            raise ChangeSetError(f"role '{self.actor.role}' cannot review or apply change sets")

    def _move(self, cs: ChangeSet, status: str, **detail: Any) -> None:
        old = cs.status
        cs.status = status
        cs.updated_at = utcnow()
        audit_log.record(
            self.session,
            self.actor.name,
            f"change_set.{status}",
            "change_set",
            cs.id,
            previous=old,
            **detail,
        )
        self.session.flush()

    def _set_findings(self, cs: ChangeSet, status: str) -> None:
        ids = [i.id for i in cs.items]
        for f in self.session.scalars(select(Finding).where(Finding.change_item_id.in_(ids))):
            f.status = status
