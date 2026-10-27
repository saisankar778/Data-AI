from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from datawright.config import Settings
from datawright.db import migrate, session_factory
from datawright.fixtures import make_fixture_repo
from datawright.models import Connection, User
from datawright.services import audit, bootstrap, connect


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(data_dir=tmp_path / "data")
    s.data_dir.mkdir()
    migrate(s.db_url)
    return s


@pytest.fixture
def sessions(settings: Settings) -> Iterator[sessionmaker[Session]]:
    factory = session_factory(settings.db_url)
    yield factory
    factory.kw["bind"].dispose()


@pytest.fixture
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    with sessions() as s:
        yield s


@pytest.fixture
def user(session: Session) -> User:
    return bootstrap(session)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_fixture_repo("retail-issues", tmp_path / "repo")


@pytest.fixture
def conn(session: Session, user: User, repo: Path) -> Connection:
    c = connect(session, user, "test", repo, "apply")
    audit(session, c)
    return c
