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
from datawright.core.changesets import ChangeSetError, ChangeSetService
from datawright.models import AgentRun, ChangeSet, Connection, Finding
from datawright.services import bootstrap

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
        error = None
        try:
            if action == "approve":
                service.approve(cs, comment)
            elif action == "reject":
                service.reject(cs, comment)
            elif action == "apply":
                service.apply(cs)
            elif action == "rollback":
                service.rollback(cs)
            else:
                raise HTTPException(404, "unknown action")
            session.commit()
        except ChangeSetError as e:
            session.rollback()
            error = str(e)
        url = f"/change-sets/{cs_id}" + (f"?error={quote(error)}" if error else "")
        return RedirectResponse(url, status_code=303)

    return app
