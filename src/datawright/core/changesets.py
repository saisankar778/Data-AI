"""Change sets: the only path to a write (docs/specs/core.md, "Change set lifecycle")."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.connectors.edits import apply_intent, check_item, replay
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
            root = Path(cs.connection.path)
            before = (root / file_path).read_bytes().decode("utf-8")
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
        item.intents = [*item.intents, intent]
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
                d = decisions.get(path, {})
                if d.get("accept", True) is False:
                    feedback.append(_fb(path, proposed, None, "rejected"))
                    rejected_paths.add(path)
                    continue
                final = d.get("value")
                if final is not None and " ".join(str(final).split()) != " ".join(
                    str(proposed or "").split()
                ):
                    keep.append(
                        {
                            **intent,
                            "value": " ".join(str(final).split()),
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
            expected = gitops.hash_content(repo, item.file_path, item.before.encode("utf-8"))
            if at_head != expected:
                raise ConflictError(
                    f"{item.file_path} at HEAD is not the version this change was made from "
                    "(it changed since, or has uncommitted edits). Commit your work and re-run."
                )
        branch = branch_name(cs)
        if gitops.branch_sha(repo, branch):
            raise ConflictError(f"branch {branch} already exists")
        message = (
            f"Datawright change set #{cs.id}: {cs.title}\n\n"
            f"Approved by {cs.reviewed_by}. Agent run #{cs.run_id}."
        )
        commit = gitops.commit_files(
            repo, head, {i.file_path: i.after.encode("utf-8") for i in cs.items}, message
        )
        gitops.update_branch(repo, branch, commit, None)
        cs.base_sha, cs.branch, cs.commit_sha, cs.error = head, branch, commit, None
        self._set_findings(cs, "fixed")
        self._move(cs, "applied", branch=branch, commit=commit)
        return commit

    def rollback(self, cs: ChangeSet) -> str:
        self._require(cs, "applied")
        self._require_approver()
        repo = Path(cs.connection.path)
        assert cs.branch
        tip = gitops.branch_sha(repo, cs.branch)
        if tip is None:
            raise ConflictError(f"branch {cs.branch} no longer exists")
        for item in cs.items:
            expected = gitops.hash_content(repo, item.file_path, item.after.encode("utf-8"))
            if gitops.blob_sha(repo, tip, item.file_path) != expected:
                raise ConflictError(f"{item.file_path} changed on {cs.branch} after the change set")
        commit = gitops.commit_files(
            repo,
            tip,
            {i.file_path: i.before.encode("utf-8") for i in cs.items},
            f"Roll back Datawright change set #{cs.id}: {cs.title}",
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
