"""Application operations shared by the CLI, the web UI and evals."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from datawright.connectors.powerbi.pbip import (
    PbipRepoConnector,
    PbixFileConnector,
    PowerBIConnector,
    find_models,
)
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


def connector_for(conn: Connection) -> PowerBIConnector:
    if conn.kind == PbipRepoConnector.kind:
        return PbipRepoConnector(Path(conn.path))
    if conn.kind == PbixFileConnector.kind:
        return PbixFileConnector(Path(conn.path))
    raise ValueError(f"unsupported connection kind {conn.kind!r}")


PBIX_READ_ONLY = (
    "A .pbix file can only be read. To let Datawright propose and apply changes, open it in "
    "Power BI Desktop, use File > Save as > Power BI Project (.pbip), then run "
    "`datawright track <folder>` and connect that folder."
)


def connect(
    session: Session, user: User, name: str, path: Path, capability: str = "propose"
) -> Connection:
    cap = Capability.parse(capability)
    connector: PowerBIConnector
    if path.is_file() and path.suffix.lower() == ".pbix":
        if cap > PbixFileConnector.max_capability:
            if capability != "propose":  # propose is the default; quietly use read for a .pbix
                raise ValueError(PBIX_READ_ONLY)
            cap = Capability.READ
        connector = PbixFileConnector(path.resolve())
    else:
        root = gitops.project_root(path)
        if root is None:
            raise ValueError(
                f"{path} is not in its own Git repository. Run `datawright track {path}` first."
            )
        connector = PbipRepoConnector(root)
    problems = connector.check()
    if problems:
        raise ValueError("; ".join(problems))
    try:
        connector.models()
    except Exception as e:  # a damaged or unsupported model must not create a connection
        raise ValueError(f"could not read the model: {e}") from e
    conn = session.scalar(select(Connection).where(Connection.name == name))
    if conn is None:
        conn = Connection(
            workspace_id=user.workspace_id,
            name=name,
            kind=connector.kind,
            path=str(connector.root),
            capability=cap.label,
        )
        session.add(conn)
    else:
        conn.kind, conn.path, conn.capability = connector.kind, str(connector.root), cap.label
    session.flush()
    audit_log.record(
        session,
        user.name,
        "connection.save",
        "connection",
        conn.id,
        path=str(connector.root),
        kind=connector.kind,
        capability=cap.label,
    )
    session.commit()
    return conn


def list_connections(session: Session) -> list[Connection]:
    return list(session.scalars(select(Connection).order_by(Connection.name)))


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
            category=f.category,
        )
        session.add(row)
        out.append(row)
    session.commit()
    return out


PBIP_GITIGNORE = """# Power BI Desktop local files
.pbi/localSettings.json
.pbi/cache.abf
"""


def track(path: Path) -> str:
    """Start tracking a PBIP folder with Git so change sets can be applied as commits."""
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a folder")
    root = gitops.project_root(path)
    if root is not None:
        return f"{path} is already tracked by Git (repository root: {root})."
    if not find_models(path):
        raise ValueError(
            f"no *.SemanticModel folder in {path}. In Power BI Desktop use File > "
            "Save as > Power BI Project (.pbip) first."
        )
    gitops.git(path, "init", "-q")
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text(PBIP_GITIGNORE, encoding="utf-8")
    gitops.git(path, "add", "-A")
    message = "Start tracking this Power BI project (Datawright)"
    try:  # prefer the user's own Git identity for their first commit
        gitops.git(path, "commit", "-q", "-m", message, as_datawright=False)
    except gitops.GitError:
        gitops.git(path, "commit", "-q", "-m", message)
    return f"Now tracking {path} with Git. Connect it with `datawright connect <name> {path}`."
