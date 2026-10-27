"""Application operations shared by the CLI, the web UI and evals."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from datawright.connectors.powerbi.pbip import PbipRepoConnector
from datawright.connectors.powerbi.rules import run_rules
from datawright.core import audit_log, gitops
from datawright.core.capabilities import Capability
from datawright.models import Asset, Connection, Finding, User, Workspace, utcnow

LOCAL_USER = "local"


def bootstrap(session: Session) -> User:
    """The local workspace and its single admin user."""
    user = session.scalar(select(User).where(User.name == LOCAL_USER))
    if user is None:
        ws = Workspace(name="Local")
        session.add(ws)
        session.flush()
        user = User(workspace_id=ws.id, name=LOCAL_USER, role="admin")
        session.add(user)
        session.commit()
    return user


def connector_for(conn: Connection) -> PbipRepoConnector:
    if conn.kind != PbipRepoConnector.kind:
        raise ValueError(f"unsupported connection kind {conn.kind!r}")
    return PbipRepoConnector(Path(conn.path))


def connect(
    session: Session, user: User, name: str, path: Path, capability: str = "propose"
) -> Connection:
    cap = Capability.parse(capability)
    if cap > PbipRepoConnector.max_capability:
        raise ValueError(
            f"a {PbipRepoConnector.kind} connection supports at most "
            f"{PbipRepoConnector.max_capability.label}"
        )
    if not gitops.is_repo(path):
        raise ValueError(
            f"{path} is not inside a Git repository. Run `git init` there and commit "
            "your PBIP files first."
        )
    root = gitops.toplevel(path)
    problems = PbipRepoConnector(root).check()
    if problems:
        raise ValueError("; ".join(problems))
    conn = session.scalar(select(Connection).where(Connection.name == name))
    if conn is None:
        conn = Connection(
            workspace_id=user.workspace_id,
            name=name,
            kind=PbipRepoConnector.kind,
            path=str(root),
            capability=cap.label,
        )
        session.add(conn)
    else:
        conn.path, conn.capability = str(root), cap.label
    session.flush()
    audit_log.record(
        session,
        user.name,
        "connection.save",
        "connection",
        conn.id,
        path=str(root),
        capability=cap.label,
    )
    session.commit()
    return conn


def get_connection(session: Session, name: str) -> Connection:
    conn = session.scalar(select(Connection).where(Connection.name == name))
    if conn is None:
        raise ValueError(f"no connection named {name!r} (create one with `datawright connect`)")
    return conn


def crawl(session: Session, conn: Connection) -> int:
    records = connector_for(conn).crawl()
    existing = {
        a.object_path: a
        for a in session.scalars(select(Asset).where(Asset.connection_id == conn.id))
    }
    seen = set()
    for r in records:
        seen.add(r.object_path)
        a = existing.get(r.object_path)
        if a is None:
            a = Asset(connection_id=conn.id, object_path=r.object_path)
            session.add(a)
        a.kind, a.name, a.file_path, a.description, a.meta = (
            r.kind,
            r.name,
            r.file_path,
            r.description,
            r.meta,
        )
        a.updated_at = utcnow()
    for path, a in existing.items():
        if path not in seen:
            session.delete(a)
    session.commit()
    return len(records)


def audit(session: Session, conn: Connection) -> list[Finding]:
    """Re-run the rules. Open findings not tied to a change set are replaced."""
    found = run_rules(connector_for(conn).models().values())
    session.execute(
        delete(Finding).where(
            Finding.connection_id == conn.id,
            Finding.status == "open",
            Finding.change_item_id.is_(None),
        )
    )
    tied = {
        (f.rule, f.object_path)
        for f in session.scalars(
            select(Finding).where(
                Finding.connection_id == conn.id,
                Finding.change_item_id.is_not(None),
                Finding.status == "open",
            )
        )
    }
    out = []
    for f in found:
        if (f.rule, f.object_path) in tied:
            continue
        row = Finding(
            connection_id=conn.id,
            object_path=f.object_path,
            source="audit",
            rule=f.rule,
            severity=f.severity,
            message=f.message,
        )
        session.add(row)
        out.append(row)
    session.commit()
    return out
