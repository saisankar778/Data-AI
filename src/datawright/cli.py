"""`datawright` command line."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.runtime import MessagesClient
from datawright.config import HOST, Settings
from datawright.db import migrate, session_factory
from datawright.models import ChangeSet, User

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Datawright: audit, fix and document Power BI models through reviewed change sets.",
)
evals_app = typer.Typer(no_args_is_help=True, help="Run eval sets.")
app.add_typer(evals_app, name="evals")


@app.callback()
def _console() -> None:
    # Windows consoles default to a legacy code page; never crash on a name we can't print.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


@contextmanager
def opened() -> Iterator[tuple[Settings, Session, User]]:
    from datawright.services import bootstrap

    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.db_url)
    with session_factory(settings.db_url)() as session:
        yield settings, session, bootstrap(session)


def make_client(settings: Settings, offline: bool, limit: int) -> tuple[MessagesClient, str]:
    from datawright.agents.documenter import offline_script
    from datawright.agents.scripted import OFFLINE_MODEL, ScriptedClient
    from datawright.evals.harness import has_credentials

    if offline:
        return ScriptedClient(offline_script(limit)), OFFLINE_MODEL
    if not has_credentials():
        typer.echo(
            "No Claude credentials found. Set ANTHROPIC_API_KEY, or add --offline to run "
            "with scripted drafts instead of Claude.",
            err=True,
        )
        raise typer.Exit(2)
    import anthropic

    return anthropic.Anthropic(), settings.model


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


@app.command()
def connect(
    name: str,
    path: Path,
    capability: str = typer.Option(
        "propose", help="read, propose or apply. Apply lets approved change sets be committed."
    ),
) -> None:
    """Connect a Git repository that contains PBIP semantic model folders."""
    from datawright.services import connect as do_connect

    with opened() as (_s, session, user):
        try:
            conn = do_connect(session, user, name, path.resolve(), capability)
        except ValueError as e:
            _fail(str(e))
        typer.echo(f"Connected '{conn.name}' ({conn.capability}) -> {conn.path}")


@app.command()
def crawl(connection: str) -> None:
    """Read model metadata (tables, columns, measures, relationships) into the catalog."""
    from datawright.services import crawl as do_crawl
    from datawright.services import get_connection

    with opened() as (_s, session, _u):
        try:
            n = do_crawl(session, get_connection(session, connection))
        except ValueError as e:
            _fail(str(e))
        typer.echo(f"Catalogued {n} objects.")


@app.command()
def audit(connection: str) -> None:
    """Run the audit rules and record findings."""
    from datawright.services import audit as do_audit
    from datawright.services import get_connection

    with opened() as (_s, session, _u):
        try:
            findings = do_audit(session, get_connection(session, connection))
        except ValueError as e:
            _fail(str(e))
        for f in findings:
            typer.echo(f"  {f.severity:8} {f.rule:34} {f.object_path}")
        typer.echo(f"{len(findings)} findings.")


@app.command()
def document(
    connection: str,
    limit: int = typer.Option(5, help="Most measures to describe."),
    offline: bool = typer.Option(False, help="Use scripted drafts instead of Claude."),
) -> None:
    """Have the agent propose descriptions for undocumented measures (as a change set)."""
    from datawright.agents.documenter import run_documenter
    from datawright.services import get_connection

    with opened() as (settings, session, user):
        client, label = make_client(settings, offline, limit)
        try:
            conn = get_connection(session, connection)
            run = run_documenter(
                session, settings, client, conn, user, limit=limit, model_label=label
            )
        except ValueError as e:
            _fail(str(e))
        _report_run(session, settings, run.id)


def _report_run(session: Session, settings: Settings, run_id: int) -> None:
    from datawright.models import AgentRun

    run = session.get(AgentRun, run_id)
    assert run is not None
    typer.echo(
        f"Run #{run.id}: {run.status} | {run.model} | {run.input_tokens} in / "
        f"{run.output_tokens} out tokens | ${run.cost_usd:.4f}"
    )
    if run.error:
        typer.echo(f"  {run.error}")
    if run.final_text:
        typer.echo(f"  Agent: {run.final_text}")
    for cs in session.scalars(select(ChangeSet).where(ChangeSet.run_id == run.id)):
        edits = sum(len(i.intents) for i in cs.items)
        typer.echo(
            f"Change set #{cs.id}: {cs.status}, {edits} edits -> "
            f"http://{HOST}:{settings.port}/change-sets/{cs.id}"
        )


@app.command()
def demo(
    offline: bool = typer.Option(False, help="Use scripted drafts instead of Claude."),
    limit: int = typer.Option(3, help="Most measures to describe."),
) -> None:
    """Copy the retail-issues fixture into a fresh Git repo and run connect, audit, document."""
    from datawright.agents.documenter import run_documenter
    from datawright.fixtures import make_fixture_repo
    from datawright.services import audit as do_audit
    from datawright.services import connect as do_connect
    from datawright.services import crawl as do_crawl

    with opened() as (settings, session, user):
        client, label = make_client(settings, offline, limit)
        repo = make_fixture_repo(
            "retail-issues", (settings.data_dir / "demo" / "retail-issues").resolve()
        )
        typer.echo(f"Demo repository: {repo}")
        conn = do_connect(session, user, "demo", repo, "apply")
        typer.echo(f"Catalogued {do_crawl(session, conn)} objects.")
        findings = do_audit(session, conn)
        typer.echo(f"Audit: {len(findings)} findings.")
        run = run_documenter(session, settings, client, conn, user, limit=limit, model_label=label)
        _report_run(session, settings, run.id)
        typer.echo("Next: run `datawright serve`, open the change set, approve it, then apply it.")


@app.command()
def serve(port: int = typer.Option(0, help="Port (default from DATAWRIGHT_PORT or 8765).")) -> None:
    """Start the local review UI on 127.0.0.1."""
    import uvicorn

    from datawright.web.app import create_app

    settings = Settings()
    if port:
        settings = settings.model_copy(update={"port": port})
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.db_url)
    app_ = create_app(settings, session_factory(settings.db_url))
    typer.echo(f"Datawright UI: http://{HOST}:{settings.port}/")
    uvicorn.run(app_, host=HOST, port=settings.port, log_level="warning")


@evals_app.command("run")
def evals_run(
    set_name: str = typer.Option(..., "--set", help="Eval set name, e.g. audit-rules-v0"),
    offline: bool = typer.Option(False, help="Use scripted drafts for model sets."),
) -> None:
    """Run an eval set, record results, and fail if it falls below its threshold."""
    from datawright.evals.harness import has_credentials, run_set

    with opened() as (settings, session, _u):
        factory = None
        if offline or has_credentials():

            def factory() -> tuple[MessagesClient, str]:
                return make_client(settings, offline, limit=50)

        result = run_set(session, settings, set_name, factory, offline)
        if result.skipped:
            typer.echo(f"{set_name}: skipped ({result.skipped})")
            return
        for c in result.cases:
            typer.echo(f"  {'PASS' if c.passed else 'FAIL'}  {c.case_id}  {c.detail}")
        typer.echo(
            f"{set_name} [{result.model}]: {result.pass_rate:.0%} passed "
            f"(threshold {result.threshold:.0%})"
        )
        if not result.ok:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
