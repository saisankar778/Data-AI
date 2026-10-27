"""`datawright` command line."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from datawright.agents.runtime import MessagesClient
from datawright.config import HOST, Settings, load_env
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
def _startup() -> None:
    # Windows consoles default to a legacy code page; never crash on a name we can't print.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
    load_env()


@contextmanager
def opened() -> Iterator[tuple[Settings, Session, User]]:
    from datawright.services import bootstrap

    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.db_url)
    with session_factory(settings.db_url)() as session:
        yield settings, session, bootstrap(session)


def make_client(settings: Settings, offline: bool, script: Any) -> tuple[MessagesClient, str]:
    """The real Claude client, or a scripted stand-in when `offline`."""
    from datawright.agents.scripted import OFFLINE_MODEL, ScriptedClient
    from datawright.evals.harness import has_credentials

    if offline:
        return ScriptedClient(script), OFFLINE_MODEL
    if not has_credentials():
        typer.echo(
            "No Claude credentials found. Put ANTHROPIC_API_KEY=... in a .env file in this "
            "folder (or set the environment variable), or add --offline to run without Claude.",
            err=True,
        )
        raise typer.Exit(2)
    import anthropic

    return anthropic.Anthropic(), settings.model


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------- health check (no connection)


@app.command()
def check(
    path: Path,
    show_all: bool = typer.Option(False, "--all", help="List every finding, not just a summary."),
    fmt: str = typer.Option("text", "--format", help="text, json, sarif or github."),
    output: Path = typer.Option(None, "--output", help="Write json/sarif output to this file."),
    fail_on: str = typer.Option(
        "none", "--fail-on", help="CI gate: exit 1 on findings at or above none/info/warning/error."
    ),
    min_score: int = typer.Option(None, "--min-score", help="CI gate: exit 1 below this score."),
) -> None:
    """Health-check a PBIP folder or a .pbix file: score, AI-readiness and top problems.
    For CI, use --format sarif/github/json with --fail-on and --min-score."""
    from datawright.connectors.powerbi import ci
    from datawright.connectors.powerbi.health import health
    from datawright.connectors.powerbi.pbip import open_source

    if fmt not in ("text", "json", "sarif", "github"):
        _fail("--format must be text, json, sarif or github")
    if fail_on not in ("none", "info", "warning", "error"):
        _fail("--fail-on must be none, info, warning or error")
    connector = open_source(path.resolve())
    try:
        models = connector.models()
    except Exception as e:  # unreadable files must give a message, not a traceback
        _fail(f"could not read {path}: {e}")
    if not models:
        _fail(f"no Power BI model found in {path} (expected a .pbix file or a PBIP folder)")
    reports = [health(m) for m in models.values()]
    if fmt == "text":
        _print_health(reports, show_all)
    else:
        render = {"json": ci.to_json, "sarif": ci.to_sarif, "github": ci.to_github}[fmt]
        text = render(connector, reports)
        if output:
            output.write_text(text, encoding="utf-8")
            typer.echo(f"Wrote {output}")
        else:
            typer.echo(text)
    reasons = ci.gate_failures(reports, fail_on, min_score)
    if reasons:
        typer.echo("Quality gate failed: " + "; ".join(reasons), err=True)
        raise typer.Exit(1)


def _print_health(reports: list[Any], show_all: bool) -> None:
    from datawright.connectors.powerbi.rules import rule_info

    for h in reports:
        typer.echo(f"\n{h.model} ({h.source}): health {h.score}/100, grade {h.grade}")
        typer.echo(f"  {h.tables} tables, {h.columns} columns, {h.measures} measures")
        typer.echo(
            f"  AI-readiness {h.ai_readiness}%: {h.documented} of {h.visible} visible "
            "objects have descriptions"
        )
        sev = h.by_severity
        typer.echo(
            f"  {sev.get('error', 0)} errors, {sev.get('warning', 0)} warnings, "
            f"{sev.get('info', 0)} info"
        )
        if show_all:
            for f in h.findings:
                typer.echo(f"  {f.severity:7} {f.rule:32} {f.message}")
            continue
        ranked = sorted(h.by_rule.items(), key=lambda x: (_sev_rank(rule_info(x[0])), -x[1]))
        typer.echo("\n  Top problems:")
        for rule, n in ranked[:10]:
            info = rule_info(rule)
            example = next(f for f in h.findings if f.rule == rule)
            typer.echo(f"  - {info.title if info else rule} ({n}): {example.message}")
        typer.echo("\n  Run with --all for every finding, or `datawright fix` for automatic fixes.")


def _sev_rank(info: Any) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(getattr(info, "severity", ""), 3)


# ---------------------------------------------------------------- connections


@app.command()
def track(path: Path, yes: bool = typer.Option(False, "--yes", help="Don't ask.")) -> None:
    """Start tracking a PBIP folder with Git (needed before Datawright can apply changes)."""
    from datawright.connectors.powerbi.pbip import find_models
    from datawright.core import gitops
    from datawright.services import track as do_track

    path = path.resolve()
    if path.is_dir() and gitops.project_root(path) is None and find_models(path) and not yes:
        typer.confirm(
            f"Create a Git repository in {path} and commit the current files?", abort=True
        )
    try:
        typer.echo(do_track(path))
    except ValueError as e:
        _fail(str(e))


@app.command()
def connect(
    name: str,
    path: Path,
    capability: str = typer.Option(
        "propose", help="read, propose or apply. Apply lets approved change sets be committed."
    ),
) -> None:
    """Connect a PBIP folder in Git (read/propose/apply) or a .pbix file (read only)."""
    from datawright.services import connect as do_connect

    with opened() as (_s, session, user):
        try:
            conn = do_connect(session, user, name, path.resolve(), capability)
        except ValueError as e:
            _fail(str(e))
        typer.echo(f"Connected '{conn.name}' ({conn.kind}, {conn.capability}) -> {conn.path}")


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
def diagnose(
    target: str = typer.Argument(..., help="A connection name, a PBIP folder or a .pbix file."),
    error: str = typer.Argument("", help="The refresh error text (or use --file)."),
    file: Path = typer.Option(None, "--file", help="Read the error text from this file."),
    explain_: bool = typer.Option(
        False, "--explain", help="Also ask Claude for a tailored explanation (needs the API key)."
    ),
) -> None:
    """Refresh doctor: recognise a refresh error and point at the tables, steps and columns
    involved in your model, with how to fix it."""
    from datawright.connectors.powerbi.pbip import open_source
    from datawright.connectors.powerbi.refresh import diagnose as run_diagnose

    text = file.read_text(encoding="utf-8", errors="replace") if file else error
    if not text.strip():
        _fail("give the error text as an argument or with --file")
    with opened() as (settings, session, user):
        from datawright.models import Connection

        conn = session.scalar(select(Connection).where(Connection.name == target))
        try:
            if conn is not None:
                from datawright.agents.refresh_doctor import diagnose_connection

                diagnoses = diagnose_connection(conn, text)
            else:
                models = list(open_source(Path(target).resolve()).models().values())
                if not models:
                    _fail(f"{target} is neither a connection nor a Power BI model")
                diagnoses = run_diagnose(text, models)
        except ValueError as e:
            _fail(str(e))
        if not diagnoses:
            typer.echo("No known refresh-error pattern matched. Try --explain for Claude's view.")
        for d in diagnoses:
            typer.echo(f"\n{d.title}  [{d.pattern}]")
            typer.echo(f"  Matched: {d.matched}")
            typer.echo(f"  Why:     {d.cause}")
            typer.echo(f"  Fix:     {d.fix}")
            for ev in d.evidence[:10]:
                typer.echo(f"  - {ev.object_path}: {ev.note}")
        if explain_:
            if conn is None:
                _fail("--explain needs a connection (run `datawright connect` first)")
            from datawright.agents.refresh_doctor import explain

            client, label = make_client(settings, False, None)
            run = explain(session, settings, client, conn, user, text, diagnoses,
                          model_label=label)  # fmt: skip
            typer.echo(f"\nClaude:\n{run.final_text or run.error}")


@app.command()
def diff(
    path: Path,
    base: str = typer.Option("HEAD", help="Git revision to compare from."),
    head: str = typer.Option("", help="Git revision to compare to (default: working tree)."),
    out: Path = typer.Option(None, help="Write Markdown to this file (e.g. a PR comment)."),
    fail_on_break: bool = typer.Option(
        False, "--fail-on-break", help="Exit 1 if the change breaks DAX references."
    ),
) -> None:
    """Explain what changed in a PBIP project between two Git versions, in model terms:
    objects added, removed or changed, references it breaks, and the health-score delta."""
    from datawright.connectors.powerbi.diff import diff_revisions, render_markdown
    from datawright.core import gitops

    repo = gitops.project_root(path.resolve())
    if repo is None:
        _fail(f"{path} is not in its own Git repository (run `datawright track` first)")
    try:
        diffs = diff_revisions(repo, base or None, head or None)
    except gitops.GitError as e:
        _fail(str(e))
    text = render_markdown(diffs, f"Model changes: {base} -> {head or 'working tree'}")
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)
    if fail_on_break and any(d.broken_references for d in diffs):
        raise typer.Exit(1)


@app.command()
def guide(
    connection: str,
    out: Path = typer.Option(None, help="Write to this file (default: print)."),
) -> None:
    """Write the model guide (data dictionary) as Markdown."""
    from datawright.connectors.powerbi.guide import render_markdown
    from datawright.connectors.powerbi.health import health
    from datawright.services import connector_for, get_connection

    with opened() as (_s, session, _u):
        try:
            models = connector_for(get_connection(session, connection)).models()
        except ValueError as e:
            _fail(str(e))
        text = "\n\n".join(render_markdown(m, health(m)) for m in models.values())
    if out:
        out.write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text)


@app.command()
def fix(
    connection: str,
    rules: str = typer.Option("", help="Only these rule ids (comma-separated)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan; change nothing."),
) -> None:
    """Propose automatic fixes for audit findings (a change set to review), and say how to fix
    the rest by hand."""
    from collections import Counter

    from datawright.connectors.powerbi.fixes import guidance
    from datawright.fixing import preview_fixes, propose_fixes
    from datawright.services import get_connection

    rule_set = {r.strip() for r in rules.split(",") if r.strip()} or None
    with opened() as (settings, session, user):
        try:
            conn = get_connection(session, connection)
            if dry_run:
                fixes, manual = preview_fixes(conn, rule_set)
                cs = None
            else:
                result = propose_fixes(session, conn, user, rule_set)
                fixes, manual, cs = result.fixes, result.manual, result.change_set
                for f, why in result.skipped:
                    typer.echo(f"  skipped {f.summary}: {why}")
        except ValueError as e:
            _fail(str(e))
        verb = "Would fix" if dry_run else "Proposed"
        typer.echo(f"{verb} {len(fixes)} findings automatically:")
        for f in fixes:
            typer.echo(f"  - {f.summary}")
        if cs is not None:
            typer.echo(
                f"Change set #{cs.id}: {cs.status} -> http://{HOST}:{settings.port}"
                f"/change-sets/{cs.id}  (review, approve, then apply)"
            )
        if manual:
            typer.echo(f"\n{len(manual)} findings need a person:")
            for rule, n in Counter(m.rule for m in manual).most_common():
                typer.echo(f"  - {rule} ({n}): {guidance(rule)}")


# ---------------------------------------------------------------- agents


@app.command()
def document(
    connection: str,
    limit: int = typer.Option(10, help="Most descriptions to propose."),
    kinds: str = typer.Option("measure,column,table", help="Which objects to document."),
    offline: bool = typer.Option(False, help="Use scripted drafts instead of Claude."),
) -> None:
    """Have the agent propose descriptions for undocumented objects (as a change set)."""
    from datawright.agents.documenter import offline_script, run_documenter
    from datawright.services import get_connection

    with opened() as (settings, session, user):
        client, label = make_client(settings, offline, offline_script(limit))
        try:
            conn = get_connection(session, connection)
            run = run_documenter(
                session,
                settings,
                client,
                conn,
                user,
                limit=limit,
                kinds=[k.strip() for k in kinds.split(",")],
                model_label=label,
            )
        except ValueError as e:
            _fail(str(e))
        _report_run(session, settings, run.id)


@app.command()
def ask(
    connection: str,
    question: str,
    offline: bool = typer.Option(False, help="Keyword search only, no Claude."),
) -> None:
    """Ask a question about a model (what a measure means, where something is defined)."""
    from datawright.agents.ask import offline_script, run_ask
    from datawright.services import get_connection

    with opened() as (settings, session, user):
        client, label = make_client(settings, offline, offline_script)
        try:
            run = run_ask(
                session,
                settings,
                client,
                get_connection(session, connection),
                user,
                question,
                model_label=label,
            )
        except ValueError as e:
            _fail(str(e))
        if run.final_text:
            typer.echo(run.final_text)
        if run.status != "succeeded":
            _fail(run.error or run.status)


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
    limit: int = typer.Option(5, help="Most descriptions to propose."),
) -> None:
    """Copy the retail-issues sample into a fresh Git repo and run connect, audit, document."""
    from datawright.agents.documenter import offline_script, run_documenter
    from datawright.fixtures import make_fixture_repo
    from datawright.services import audit as do_audit
    from datawright.services import connect as do_connect
    from datawright.services import crawl as do_crawl

    with opened() as (settings, session, user):
        client, label = make_client(settings, offline, offline_script(limit))
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
        typer.echo(
            "Next: run `datawright serve`, open the change set, review each edit, approve, "
            "then apply."
        )


# ---------------------------------------------------------------- servers


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


@app.command()
def mcp() -> None:
    """Run the Datawright MCP server on stdio (for Claude Desktop, Claude Code, VS Code)."""
    from datawright.mcp_server import build_server

    build_server().run("stdio")


@evals_app.command("run")
def evals_run(
    set_name: str = typer.Option(..., "--set", help="Eval set name, e.g. audit-rules-v1"),
    offline: bool = typer.Option(False, help="Use scripted drafts for model sets."),
) -> None:
    """Run an eval set, record results, and fail if it falls below its threshold."""
    from datawright.evals.harness import has_credentials, run_set

    with opened() as (settings, session, _u):
        factory = None
        if offline or has_credentials():

            def factory(script: Any) -> tuple[MessagesClient, str]:
                return make_client(settings, offline, script)

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
