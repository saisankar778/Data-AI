from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from datawright.agents.documenter import offline_script, run_documenter
from datawright.agents.scripted import ScriptedClient
from datawright.config import Settings
from datawright.core import gitops
from datawright.models import ChangeSet, Connection, User
from datawright.web.app import create_app


@pytest.fixture
def client(settings: Settings, sessions: sessionmaker[Session]) -> TestClient:
    return TestClient(create_app(settings, sessions, allowed_hosts={"testserver"}))


@pytest.fixture
def cs_id(session: Session, settings: Settings, user: User, conn: Connection) -> int:
    run = run_documenter(session, settings, ScriptedClient(offline_script(2)), conn, user, limit=2)
    cs = session.query(ChangeSet).filter_by(run_id=run.id).one()
    return cs.id


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m
    return m.group(1)


def test_pages_render(client: TestClient, cs_id: int) -> None:
    assert "Overview" in client.get("/").text
    page = client.get(f"/change-sets/{cs_id}").text
    assert "proposed" in page and "Offline draft" in page and 'class="add"' in page
    assert "propose_description" in client.get("/runs/1").text
    assert "measure-missing-description" in client.get("/findings").text


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


def test_approve_apply_rollback_through_the_ui(
    client: TestClient, cs_id: int, repo: Path, session: Session
) -> None:
    token = csrf_of(client.get(f"/change-sets/{cs_id}").text)
    for action in ("approve", "apply"):
        r = client.post(
            f"/change-sets/{cs_id}/{action}",
            data={"csrf": token, "comment": "ok"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "error" not in r.headers["location"]
    page = client.get(f"/change-sets/{cs_id}").text
    assert "applied" in page and f"datawright/cs-{cs_id}" in page
    assert gitops.branch_sha(repo, f"datawright/cs-{cs_id}") is not None
    assert gitops.out(repo, "status", "--porcelain") == ""

    r = client.post(f"/change-sets/{cs_id}/rollback", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert "rolled_back" in client.get(f"/change-sets/{cs_id}").text


def test_errors_are_shown_not_raised(client: TestClient, cs_id: int) -> None:
    token = csrf_of(client.get(f"/change-sets/{cs_id}").text)
    r = client.post(f"/change-sets/{cs_id}/apply", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "expected" in client.get(r.headers["location"]).text
