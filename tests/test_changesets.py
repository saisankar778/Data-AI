from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from datawright.core import gitops
from datawright.core.changesets import ChangeSetError, ChangeSetService, ConflictError
from datawright.models import ChangeSet, Connection, User

SALES = "Retail.SemanticModel/definition/tables/Sales.tmdl"


def intent(name: str, value: str) -> dict[str, str]:
    return {
        "op": "tmdl.set_description",
        "kind": "measure",
        "table": "Sales",
        "name": name,
        "value": value,
        "object_path": f"Retail/Sales/measure/{name}",
    }


def proposed(session: Session, user: User, conn: Connection) -> ChangeSet:
    svc = ChangeSetService(session, user)
    cs = svc.create_draft(conn, "Describe two measures")
    svc.add_edit(cs, SALES, intent("Total Sales", "Total sales amount."))
    svc.add_edit(cs, SALES, intent("Profit", "Sales minus cost."))
    assert svc.propose(cs) == []
    return cs


def test_edits_to_one_file_share_one_item(session: Session, user: User, conn: Connection) -> None:
    cs = proposed(session, user, conn)
    assert cs.status == "proposed"
    assert len(cs.items) == 1
    assert [i["name"] for i in cs.items[0].intents] == ["Total Sales", "Profit"]
    assert cs.items[0].diff.count("\n+\t///") == 2


def test_apply_commits_to_a_branch_without_touching_the_working_tree(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    head_before = gitops.head_sha(repo)
    on_disk_before = (repo / SALES).read_bytes()
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.approve(cs, "looks right")
    commit = svc.apply(cs)

    assert cs.status == "applied" and cs.branch == f"datawright/cs-{cs.id}"
    assert gitops.branch_sha(repo, cs.branch) == commit
    assert gitops.out(repo, "rev-parse", f"{commit}^") == head_before
    assert gitops.head_sha(repo) == head_before
    assert gitops.current_branch(repo) == "main"
    assert gitops.out(repo, "status", "--porcelain") == ""
    assert (repo / SALES).read_bytes() == on_disk_before
    committed = gitops.show(repo, commit, SALES).decode()
    assert "\t/// Total sales amount.\n\tmeasure 'Total Sales'" in committed
    assert committed == cs.items[0].after
    changed = gitops.out(repo, "diff", "--name-only", head_before, commit)
    assert changed == SALES


def test_rollback_restores_before_state(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.approve(cs)
    svc.apply(cs)
    rollback = svc.rollback(cs)
    assert cs.status == "rolled_back"
    assert gitops.branch_sha(repo, cs.branch or "") == rollback
    assert gitops.show(repo, rollback, SALES).decode() == cs.items[0].before


def test_apply_refuses_when_file_changed_since_proposal(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.approve(cs)
    (repo / SALES).write_bytes((repo / SALES).read_bytes() + b"\n")
    gitops.git(repo, "commit", "-qam", "someone else edited Sales")
    with pytest.raises(ConflictError):
        svc.apply(cs)
    assert cs.status == "approved"


def test_uncommitted_edits_are_a_conflict(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    (repo / SALES).write_bytes((repo / SALES).read_bytes() + b"\n")  # dirty, not committed
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.approve(cs)
    with pytest.raises(ConflictError):
        svc.apply(cs)


def margin_sets(session: Session, user: User, conn: Connection) -> tuple[ChangeSet, ChangeSet]:
    """The Phase 1 Desktop check: `fix` rewrote 'Margin %' and `document` described it, both
    proposed from the same HEAD. As sibling branches they conflicted when merged."""
    svc = ChangeSetService(session, user)
    fix = svc.create_draft(conn, "Use DIVIDE")
    svc.add_edit(fix, SALES, {
        "op": "tmdl.set_expression", "kind": "measure", "table": "Sales", "name": "Margin %",
        "value": "DIVIDE([Profit], [Total Sales])", "object_path": "Retail/Sales/measure/Margin %",
    })  # fmt: skip
    doc = svc.create_draft(conn, "Describe Margin %")
    svc.add_edit(doc, SALES, intent("Margin %", "Profit as a share of total sales."))
    for cs in (fix, doc):
        assert svc.propose(cs) == []
        svc.approve(cs)
    return fix, doc


def test_second_change_set_stacks_on_the_first(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    head = gitops.head_sha(repo)
    fix, doc = margin_sets(session, user, conn)
    svc = ChangeSetService(session, user)
    first = svc.apply(fix)
    second = svc.apply(doc)

    assert gitops.out(repo, "rev-parse", f"{first}^") == head
    assert gitops.out(repo, "rev-parse", f"{second}^") == first
    assert f"#{fix.id}" in gitops.out(repo, "log", "-1", "--format=%B", second)
    text = gitops.show(repo, second, SALES).decode()
    assert (
        "\t/// Profit as a share of total sales.\n"
        "\tmeasure 'Margin %' = DIVIDE([Profit], [Total Sales])\n"
    ) in text
    item = doc.items[0]
    assert item.before == gitops.show(repo, first, SALES).decode() and item.after == text
    changed = [ln for ln in item.diff.splitlines()[2:] if ln[:1] in "+-"]
    assert changed == ["+\t/// Profit as a share of total sales."]  # the review shows doc only
    assert gitops.head_sha(repo) == head and gitops.out(repo, "status", "--porcelain") == ""
    # Merging the newest branch alone brings in both sets.
    gitops.git(repo, "merge", "--ff-only", "-q", doc.branch or "", as_datawright=False)
    assert (repo / SALES).read_bytes().decode() == text


def test_merged_change_sets_are_not_stacked_on(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    svc = ChangeSetService(session, user)
    cs = proposed(session, user, conn)
    svc.approve(cs)
    svc.apply(cs)
    gitops.git(repo, "merge", "--ff-only", "-q", cs.branch or "", as_datawright=False)
    later = svc.create_draft(conn, "Describe Units")
    svc.add_edit(later, SALES, intent("Units ", "Number of units sold."))
    assert svc.propose(later) == []
    svc.approve(later)
    commit = svc.apply(later)
    assert gitops.out(repo, "rev-parse", f"{commit}^") == gitops.head_sha(repo)


def test_rollback_waits_for_sets_stacked_on_top(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    fix, doc = margin_sets(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.apply(fix)
    svc.apply(doc)
    with pytest.raises(ConflictError, match=f"#{doc.id}"):
        svc.rollback(fix)
    svc.rollback(doc)
    assert "DIVIDE" in gitops.show(repo, doc.branch or "", SALES).decode()  # only doc undone
    svc.rollback(fix)
    assert fix.status == "rolled_back"


def test_review_rules(session: Session, user: User, conn: Connection) -> None:
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    with pytest.raises(ChangeSetError):
        svc.apply(cs)  # not approved yet
    with pytest.raises(ChangeSetError):
        svc.reject(cs, "  ")  # a rejection needs a reason
    viewer = User(workspace_id=user.workspace_id, name="viewer", role="viewer")
    session.add(viewer)
    session.flush()
    with pytest.raises(ChangeSetError):
        ChangeSetService(session, viewer).approve(cs)
    svc.reject(cs, "wrong wording")
    assert cs.status == "rejected" and cs.review_comment == "wrong wording"


def test_apply_needs_apply_capability(session: Session, user: User, conn: Connection) -> None:
    conn.capability = "propose"
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    svc.approve(cs)
    with pytest.raises(ChangeSetError, match="not 'apply'"):
        svc.apply(cs)


def test_tampered_item_fails_validation(session: Session, user: User, conn: Connection) -> None:
    svc = ChangeSetService(session, user)
    cs = svc.create_draft(conn, "tampered")
    item = svc.add_edit(cs, SALES, intent("Total Sales", "Total sales."))
    item.after = item.after.replace("formatString: 0.0%", "formatString: 0%", 1)
    problems = svc.propose(cs)
    assert problems and cs.status == "draft"


def test_audit_log_is_insert_only(session: Session, user: User, conn: Connection) -> None:
    proposed(session, user, conn)
    session.commit()
    assert session.scalar(sa.text("SELECT count(*) FROM audit_log")) > 0
    with pytest.raises(sa.exc.DatabaseError):
        session.execute(sa.text("UPDATE audit_log SET actor = 'someone else'"))
    session.rollback()
    with pytest.raises(sa.exc.DatabaseError):
        session.execute(sa.text("DELETE FROM audit_log"))


def test_review_edit_by_edit(session: Session, user: User, conn: Connection, repo: Path) -> None:
    svc = ChangeSetService(session, user)
    cs = svc.create_draft(conn, "three edits")
    svc.add_edit(cs, SALES, intent("Total Sales", "Total sales amount."))
    svc.add_edit(cs, SALES, intent("Profit", "Sales minus cost."))
    svc.add_edit(cs, SALES, intent("Total Cost", "Cost of goods."))
    assert svc.propose(cs) == []
    svc.approve(cs, "partly", decisions={
        "Retail/Sales/measure/Profit": {"accept": False},
        "Retail/Sales/measure/Total Cost": {"accept": True, "value": "Cost of goods sold."},
    })  # fmt: skip
    assert cs.status == "approved"
    assert (cs.proposed_count, cs.accepted_count, cs.edited_count, cs.rejected_count) == (
        3,
        1,
        1,
        1,
    )
    values = {i["name"]: i["value"] for i in cs.items[0].intents}
    assert values == {"Total Sales": "Total sales amount.", "Total Cost": "Cost of goods sold."}
    edited = next(f for f in cs.feedback if f["decision"] == "edited")
    assert edited["proposed"] == "Cost of goods." and edited["final"] == "Cost of goods sold."
    after = cs.items[0].after
    assert "/// Cost of goods sold." in after and "Sales minus cost." not in after
    svc.apply(cs)
    committed = gitops.show(repo, cs.commit_sha or "", SALES).decode()
    assert committed == after


def test_rejecting_every_edit_is_refused(session: Session, user: User, conn: Connection) -> None:
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    reject_all = {i["object_path"]: {"accept": False} for i in cs.items[0].intents}
    with pytest.raises(ChangeSetError, match="every edit was rejected"):
        svc.approve(cs, decisions=reject_all)


def test_invalid_edit_is_refused(session: Session, user: User, conn: Connection) -> None:
    cs = proposed(session, user, conn)
    svc = ChangeSetService(session, user)
    with pytest.raises(ChangeSetError):
        svc.approve(cs, decisions={"Retail/Sales/measure/Profit": {"value": "``` nope"}})


def test_reject_records_feedback(session: Session, user: User, conn: Connection) -> None:
    cs = proposed(session, user, conn)
    ChangeSetService(session, user).reject(cs, "too vague")
    assert cs.rejected_count == 2 and cs.proposed_count == 2
    assert {f["decision"] for f in cs.feedback} == {"rejected"}


def test_home_folder_repository_is_not_a_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    project = home / "Documents" / "report"
    project.mkdir(parents=True)
    gitops.git(home, "init", "-q")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert gitops.is_repo(project)
    assert gitops.project_root(project) is None  # an accidental repo in the home folder
    gitops.git(project, "init", "-q")
    assert gitops.project_root(project) == project.resolve()
