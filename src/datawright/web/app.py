"""Local review UI. Binds to 127.0.0.1 only; every POST needs the per-process CSRF token."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from datawright.config import HOST, Settings
from datawright.connectors.powerbi.health import health
from datawright.connectors.powerbi.pbip import open_source
from datawright.connectors.powerbi.rules import rule_info
from datawright.core.changesets import ChangeSetError, ChangeSetService
from datawright.models import AgentRun, ChangeSet, Connection, Finding
from datawright.services import bootstrap, connector_for

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _db(request: Request) -> Iterator[Session]:
    with request.app.state.sessions() as session:
        yield session


DB = Annotated[Session, Depends(_db)]


def create_app(
    settings: Settings, sessions: sessionmaker[Session], allowed_hosts: set[str] | None = None
) -> FastAPI:
    app = FastAPI(title="Datawright", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sessions = sessions
    csrf_token = secrets.token_urlsafe(32)
    hosts = allowed_hosts or {f"{HOST}:{settings.port}", f"localhost:{settings.port}"}
    origins = {f"http://{h}" for h in hosts}

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host", "") not in hosts:
            return PlainTextResponse("Forbidden host", status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                return PlainTextResponse("Forbidden origin", status_code=403)
        return await call_next(request)

    def render(request: Request, name: str, **ctx: object) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"csrf": csrf_token, **ctx})

    def get_cs(session: Session, cs_id: int) -> ChangeSet:
        cs = session.get(ChangeSet, cs_id)
        if cs is None:
            raise HTTPException(404, "change set not found")
        return cs

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: DB) -> HTMLResponse:
        reviewed = session.execute(
            select(
                func.coalesce(func.sum(ChangeSet.proposed_count), 0),
                func.coalesce(func.sum(ChangeSet.accepted_count), 0),
                func.coalesce(func.sum(ChangeSet.edited_count), 0),
            )
        ).one()
        return render(
            request,
            "dashboard.html",
            connections=session.scalars(select(Connection).order_by(Connection.name)).all(),
            runs=session.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(20)).all(),
            change_sets=session.scalars(
                select(ChangeSet).order_by(ChangeSet.id.desc()).limit(20)
            ).all(),
            open_findings=session.scalar(
                select(func.count()).select_from(Finding).where(Finding.status == "open")
            ),
            reviewed={"proposed": reviewed[0], "accepted": reviewed[1], "edited": reviewed[2]},
        )

    @app.get("/connections/{conn_id}", response_class=HTMLResponse)
    def connection_detail(conn_id: int, request: Request, session: DB) -> HTMLResponse:
        conn = session.get(Connection, conn_id)
        if conn is None:
            raise HTTPException(404, "connection not found")
        try:
            reports = [health(m) for m in connector_for(conn).models().values()]
            error = None
        except Exception as e:  # show unreadable models as a message, not a crash
            reports, error = [], f"Could not read the model: {e}"
        return render(
            request, "health.html", conn=conn, reports=reports, error=error, info=rule_info
        )

    @app.get("/connections/{conn_id}/guide", response_class=HTMLResponse)
    def connection_guide(conn_id: int, request: Request, session: DB) -> HTMLResponse:
        conn = session.get(Connection, conn_id)
        if conn is None:
            raise HTTPException(404, "connection not found")
        models = list(connector_for(conn).models().values())
        return render(request, "guide.html", conn=conn, guides=[(m, health(m)) for m in models])

    @app.get("/check", response_class=HTMLResponse)
    def check_page(request: Request, path: str = "") -> HTMLResponse:
        reports, error = [], None
        if path.strip():
            try:
                models = open_source(Path(path.strip().strip('"'))).models()
                reports = [health(m) for m in models.values()]
                if not reports:
                    error = "No Power BI model found there (expected a .pbix or a PBIP folder)."
            except Exception as e:
                error = f"Could not read {path}: {e}"
        return render(
            request,
            "health.html",
            conn=None,
            reports=reports,
            error=error,
            info=rule_info,
            path=path,
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int, request: Request, session: DB) -> HTMLResponse:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        cs = session.scalars(select(ChangeSet).where(ChangeSet.run_id == run.id)).all()
        return render(request, "run.html", run=run, change_sets=cs)

    @app.get("/change-sets/{cs_id}", response_class=HTMLResponse)
    def change_set_detail(
        cs_id: int, request: Request, session: DB, error: str | None = None
    ) -> HTMLResponse:
        cs = get_cs(session, cs_id)
        findings = session.scalars(
            select(Finding).where(Finding.change_item_id.in_([i.id for i in cs.items]))
        ).all()
        return render(request, "change_set.html", cs=cs, findings=findings, error=error)

    @app.get("/findings", response_class=HTMLResponse)
    def findings(request: Request, session: DB) -> HTMLResponse:
        rows = session.scalars(
            select(Finding).order_by(Finding.status, Finding.rule, Finding.object_path)
        ).all()
        return render(request, "findings.html", findings=rows)

    def finish(session: Session, cs_id: int, action: Callable[[], object]) -> Response:
        error = None
        try:
            action()
            session.commit()
        except ChangeSetError as e:
            session.rollback()
            error = str(e)
        url = f"/change-sets/{cs_id}" + (f"?error={quote(error)}" if error else "")
        return RedirectResponse(url, status_code=303)

    @app.post("/change-sets/{cs_id}/approve")
    async def approve(cs_id: int, request: Request, session: DB) -> Response:
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf_token):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        cs = get_cs(session, cs_id)
        decisions: dict[str, dict[str, object]] = {}
        n = 0
        while f"path_{n}" in form:
            path = str(form[f"path_{n}"])
            decisions[path] = {
                "accept": f"keep_{n}" in form,
                "value": str(form.get(f"value_{n}", "")) or None,
            }
            n += 1
        service = ChangeSetService(session, bootstrap(session))
        comment = str(form.get("comment", ""))
        return finish(session, cs_id, lambda: service.approve(cs, comment, decisions))

    @app.post("/change-sets/{cs_id}/{action}")
    def act(
        cs_id: int,
        action: str,
        session: DB,
        csrf: Annotated[str, Form()] = "",
        comment: Annotated[str, Form()] = "",
    ) -> Response:
        if not secrets.compare_digest(csrf, csrf_token):
            return PlainTextResponse("Missing or wrong CSRF token", status_code=403)
        cs = get_cs(session, cs_id)
        service = ChangeSetService(session, bootstrap(session))
        actions: dict[str, Callable[[], object]] = {
            "reject": lambda: service.reject(cs, comment),
            "apply": lambda: service.apply(cs),
            "rollback": lambda: service.rollback(cs),
        }
        if action not in actions:
            raise HTTPException(404, "unknown action")
        return finish(session, cs_id, actions[action])

    return app
