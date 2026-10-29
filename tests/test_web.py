from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from datawright.agents.documenter import offline_script, run_documenter
from datawright.agents.scripted import ScriptedClient
from datawright.config import FIXTURES_DIR, Settings
from datawright.core import gitops
from datawright.core.changesets import ChangeSetService
from datawright.models import ChangeSet, Connection, User
from datawright.web.app import create_app


@pytest.fixture
def client(settings: Settings, sessions: sessionmaker[Session]) -> TestClient:
    return TestClient(create_app(settings, sessions, allowed_hosts={"testserver"}))


@pytest.fixture
def cs_id(session: Session, settings: Settings, user: User, conn: Connection) -> int:
    run = run_documenter(
        session, settings, ScriptedClient(offline_script(3)), conn, user, limit=3,
        kinds=["measure"],
    )  # fmt: skip
    cs = session.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))
    assert cs is not None
    return cs.id


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m
    return m.group(1)


def form_of(html: str) -> dict[str, str]:
    """The approve form's fields as a browser would send them (checked boxes only)."""
    fields = {"csrf": csrf_of(html)}
    for name, value in re.findall(r'name="(id_\d+)" value="([^"]*)"', html):
        fields[name] = value.replace("&#39;", "'").replace("&amp;", "&")
    for n in re.findall(r'name="keep_(\d+)" checked', html):
        fields[f"keep_{n}"] = "on"
    for name, value in re.findall(r'<textarea name="(value_\d+)"[^>]*>([^<]*)</textarea>', html):
        fields[name] = value
    return fields


def test_stacked_change_set_says_so(
    client: TestClient, session: Session, user: User, conn: Connection
) -> None:
    svc = ChangeSetService(session, user)
    sets = []
    for name in ("Total Sales", "Profit"):
        cs = svc.create_draft(conn, f"Describe {name}")
        svc.add_edit(cs, "Retail.SemanticModel/definition/tables/Sales.tmdl", {
            "op": "tmdl.set_description", "kind": "measure", "table": "Sales", "name": name,
            "value": f"About {name}.", "object_path": f"Retail/Sales/measure/{name}",
        })  # fmt: skip
        assert svc.propose(cs) == []
        svc.approve(cs)
        sets.append(cs)
    for cs in sets:
        svc.apply(cs)
    session.commit()
    first, second = sets
    assert "Stacked on" not in client.get(f"/change-sets/{first.id}").text
    page = client.get(f"/change-sets/{second.id}").text
    assert f'Stacked on <a href="/change-sets/{first.id}">' in page
    assert "merge only this one" in page


def test_copilot_edits_review_and_keep_markdown_lines(
    client: TestClient, session: Session, settings: Settings, user: User, conn: Connection
) -> None:
    from datawright.agents.prep_for_ai import offline_script as prep_script
    from datawright.agents.prep_for_ai import run_prep

    run = run_prep(session, settings, ScriptedClient(prep_script(5)), conn, user)
    cs = session.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))
    assert cs is not None
    page = client.get(f"/change-sets/{cs.id}").text
    assert "Copilot synonyms" in page and "Copilot AI instructions" in page
    assert "Copilot example prompts" in page and "<li>" in page
    form = form_of(page)
    n = next(k[6:] for k, v in form.items() if k.startswith("value_") and "Offline draft" in v)
    form[f"value_{n}"] = "# Notes\r\n\r\n- Sales means [Total Sales].  \r\n- Dates: Calendar.\r\n"
    assert client.post(f"/change-sets/{cs.id}/approve", data=form).status_code == 200
    session.expire_all()
    item = next(i for i in cs.items if i.file_path.endswith("instructions.md"))
    assert item.after == "# Notes\n\n- Sales means [Total Sales].\n- Dates: Calendar.\n"
    assert cs.status == "approved" and cs.edited_count == 1


def test_pages_render(client: TestClient, cs_id: int, conn: Connection) -> None:
    assert "Overview" in client.get("/").text
    page = client.get(f"/change-sets/{cs_id}").text
    assert "proposed" in page and "Offline draft" in page and 'class="add"' in page
    assert "Approve the kept edits" in page
    assert "propose_description" in client.get("/runs/1").text
    assert "measure-missing-description" in client.get("/findings").text
    health = client.get(f"/connections/{conn.id}").text
    assert "health score" in health and "Hard-code" not in health
    assert "Don&#39;t hard-code local file paths in Power Query" in health
    guide = client.get(f"/connections/{conn.id}/guide").text
    assert "Model guide: Retail" in guide and "Order Lines" in guide


def test_check_page_reads_any_path(client: TestClient) -> None:
    page = client.get("/check", params={"path": str(FIXTURES_DIR / "pbip" / "retail-clean")}).text
    assert ">100<" in page and "No problems found." in page
    pbix = client.get(
        "/check", params={"path": str(FIXTURES_DIR / "pbix" / "Revenue Opportunities.pbix")}
    )
    assert "Revenue Opportunities" in pbix.text and "(pbix)" in pbix.text
    missing = client.get("/check", params={"path": "C:/nope/missing"}).text
    assert "No Power BI model found" in missing


def test_foreign_hosts_and_origins_are_refused(client: TestClient, cs_id: int) -> None:
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403
    token = csrf_of(client.get(f"/change-sets/{cs_id}").text)
    r = client.post(
        f"/change-sets/{cs_id}/approve",
        data={"csrf": token},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    r = client.post(f"/change-sets/{cs_id}/approve", data={"csrf": "wrong"}, follow_redirects=False)
    assert r.status_code == 403
    r = client.post(f"/change-sets/{cs_id}/apply", data={"csrf": "wrong"}, follow_redirects=False)
    assert r.status_code == 403


def test_review_each_edit_then_apply_and_roll_back(
    client: TestClient, cs_id: int, repo: Path, session: Session
) -> None:
    page = client.get(f"/change-sets/{cs_id}").text
    form = form_of(page)
    assert {"id_0", "id_1", "id_2", "keep_0", "keep_1", "keep_2"} <= set(form)
    del form["keep_1"]  # reject the second edit
    form["value_2"] = "Edited by a reviewer."
    r = client.post(f"/change-sets/{cs_id}/approve", data=form, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    page = client.get(f"/change-sets/{cs_id}").text
    assert "1 accepted as proposed" in page and "1 edited" in page and "1 rejected" in page

    token = csrf_of(page)
    r = client.post(f"/change-sets/{cs_id}/apply", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    page = client.get(f"/change-sets/{cs_id}").text
    assert "applied" in page and f"datawright/cs-{cs_id}" in page
    sha = gitops.branch_sha(repo, f"datawright/cs-{cs_id}")
    assert sha is not None
    assert "Edited by a reviewer." in gitops.git(repo, "show", sha).decode()
    assert gitops.out(repo, "status", "--porcelain") == ""
    assert "of proposed edits" in client.get("/").text  # acceptance metric on the dashboard

    r = client.post(f"/change-sets/{cs_id}/rollback", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "rolled_back" in client.get(f"/change-sets/{cs_id}").text


def test_errors_are_shown_not_raised(client: TestClient, cs_id: int) -> None:
    token = csrf_of(client.get(f"/change-sets/{cs_id}").text)
    r = client.post(f"/change-sets/{cs_id}/apply", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "expected" in client.get(r.headers["location"]).text
