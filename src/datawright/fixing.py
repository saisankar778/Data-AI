"""Propose automatic fixes for audit findings as one change set (no model needed)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.connectors.powerbi.fixes import Fix, plan_fixes
from datawright.connectors.powerbi.pbip import PbixFileConnector
from datawright.connectors.powerbi.rules import RuleFinding, run_rules
from datawright.core.capabilities import Capability
from datawright.core.changesets import ChangeSetService
from datawright.models import ChangeItem, ChangeSet, Connection, Finding, User
from datawright.services import PBIX_READ_ONLY, audit, connector_for


@dataclass
class FixResult:
    change_set: ChangeSet | None
    fixes: list[Fix] = field(default_factory=list)
    manual: list[RuleFinding] = field(default_factory=list)
    skipped: list[tuple[Fix, str]] = field(default_factory=list)  # planned but failed to apply


def preview_fixes(
    conn: Connection, rules: set[str] | None = None
) -> tuple[list[Fix], list[RuleFinding]]:
    connector = connector_for(conn)
    return plan_fixes(connector, run_rules(connector.models().values()), rules)


def propose_fixes(
    session: Session, conn: Connection, user: User, rules: set[str] | None = None
) -> FixResult:
    connector = connector_for(conn)
    if isinstance(connector, PbixFileConnector):
        raise ValueError(PBIX_READ_ONLY)
    if Capability.parse(conn.capability) < Capability.PROPOSE:
        raise ValueError(f"connection {conn.name!r} is read-only; it can't take proposals")
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    audit(session, conn)  # refresh findings so the fixes can be linked to them
    fixes, manual = plan_fixes(connector, run_rules(connector.models().values()), rules)
    result = FixResult(None, [], manual)
    if not fixes:
        return result
    service = ChangeSetService(session, user)
    cs = service.create_draft(conn, f"Fix {len(fixes)} audit findings in {conn.name}")
    for fix in fixes:
        item = _item_for(cs, fix.file_path)
        snapshot = (list(item.intents), item.after, item.diff) if item else None
        try:
            for intent in fix.intents:
                service.add_edit(cs, fix.file_path, intent)
            result.fixes.append(fix)
        except ValueError as e:  # undo this fix's partial edits, keep the others
            result.skipped.append((fix, str(e)))
            item = _item_for(cs, fix.file_path)
            if item is not None:
                if snapshot is None:
                    item.intents, item.after, item.diff = [], item.before, ""
                else:
                    item.intents, item.after, item.diff = snapshot
    for item in [i for i in cs.items if not i.intents]:
        cs.items.remove(item)
        session.delete(item)
    if not cs.items:
        session.rollback()
        return result
    errors = service.propose(cs)
    if errors:
        raise ValueError("fixes failed validation: " + "; ".join(errors))
    _link(session, conn, cs)
    session.commit()
    result.change_set = cs
    return result


def _item_for(cs: ChangeSet, file_path: str) -> ChangeItem | None:
    return next((i for i in cs.items if i.file_path == file_path), None)


def _link(session: Session, conn: Connection, cs: ChangeSet) -> None:
    for item in cs.items:
        for intent in item.intents:
            rule = intent.get("rule")
            if not rule:
                continue
            finding = session.scalar(
                select(Finding).where(
                    Finding.connection_id == conn.id, Finding.rule == rule,
                    Finding.object_path == _finding_path(intent), Finding.status == "open",
                    Finding.change_item_id.is_(None),
                )
            )  # fmt: skip
            if finding is not None:
                finding.change_item_id = item.id
            if rule == "mark-date-table":  # the model-level finding is fixed by the same edit
                model_finding = session.scalar(
                    select(Finding).where(
                        Finding.connection_id == conn.id, Finding.rule == "model-has-date-table",
                        Finding.object_path == _finding_path(intent).split("/")[0],
                        Finding.status == "open", Finding.change_item_id.is_(None),
                    )
                )  # fmt: skip
                if model_finding is not None:
                    model_finding.change_item_id = item.id


def _finding_path(intent: dict[str, object]) -> str:
    """mark-date-table findings sit on the table even when the edit is on its date column."""
    path = str(intent.get("object_path", ""))
    if intent.get("rule") == "mark-date-table" and "/column/" in path:
        return path.split("/column/")[0]
    return path
