from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from datawright.agents.prep_for_ai import offline_script, run_prep
from datawright.agents.scripted import ScriptedClient
from datawright.config import Settings
from datawright.connectors.edits import check_item, replay
from datawright.connectors.powerbi import copilot
from datawright.core import gitops
from datawright.core.changesets import ChangeSetService
from datawright.mcp_server import DatawrightTools
from datawright.models import ChangeSet, Connection, User

MS = Path(__file__).parent / "data" / "ms-schemas"
MODEL = "Retail.SemanticModel"
SCHEMA = f"{MODEL}/{copilot.SCHEMA_FILE}"
INSTRUCTIONS = f"{MODEL}/{copilot.INSTRUCTIONS_FILE}"


def official(name: str) -> dict[str, Any]:
    return json.loads((MS / f"copilot-{name}-1.0.0.json").read_text(encoding="utf-8"))


def syn(kind: str, name: str, synonyms: list[str], table: str = "Sales") -> dict[str, Any]:
    return {"op": "copilot.set_synonyms", "kind": kind, "table": table, "name": name,
            "synonyms": synonyms, "object_path": f"Retail/{table}/{kind}/{name}"}  # fmt: skip


def test_synonyms_build_a_valid_ai_data_schema() -> None:
    intents = [
        syn("measure", "Total Sales", ["revenue", " Revenue ", "turnover", "total sales"]),
        syn("table", "Customer", ["clients"], table="Customer"),
        syn("column", "Amount", ["line amount"]),
    ]
    text = replay("", intents)
    data = json.loads(text)
    jsonschema.validate(data, official("schema"))
    assert data["$schema"] == copilot.SCHEMA_URL and text.endswith("}\n")
    sales = next(t for t in data["tables"] if t["name"] == "Sales")
    # Trimmed, de-duplicated case-insensitively, and never the object's own name.
    assert sales["measures"] == [{"name": "Total Sales", "synonyms": ["revenue", "turnover"]}]
    assert sales["columns"] == [{"name": "Amount", "synonyms": ["line amount"]}]
    assert check_item("", text, intents) == []


def test_existing_schema_keeps_everything_it_does_not_touch() -> None:
    before = json.dumps({
        "$schema": copilot.SCHEMA_URL,
        "tables": [
            {"name": "Product", "visibility": "Hidden",
             "columns": [{"name": "Code", "index": "None"}]},
            {"name": "Sales", "measures": [{"name": "Units", "synonyms": ["quantity"]}]},
        ],
    }, indent=2) + "\n"  # fmt: skip
    intent = syn("measure", "Total Sales", ["revenue"])
    after = replay(before, [intent])
    data = json.loads(after)
    assert data["tables"][0] == json.loads(before)["tables"][0]
    assert data["tables"][1]["measures"] == [
        {"name": "Units", "synonyms": ["quantity"]},
        {"name": "Total Sales", "synonyms": ["revenue"]},
    ]
    assert check_item(before, after, [intent]) == []
    removed = replay(after, [syn("measure", "Units", [])])
    assert json.loads(removed)["tables"][1]["measures"][0] == {"name": "Units"}


def test_check_catches_changes_beyond_the_edit() -> None:
    intent = syn("measure", "Total Sales", ["revenue"])
    after = json.loads(replay("", [intent]))
    after["tables"].append({"name": "Sneaky"})
    assert "the file changed beyond the requested edits" in check_item(
        "", json.dumps(after, indent=2) + "\n", [intent]
    )


def test_limits_come_from_power_bi_desktop() -> None:
    with pytest.raises(copilot.CopilotError, match="15000"):
        copilot.clean_instructions("x" * 15001)
    with pytest.raises(copilot.CopilotError, match="500"):
        copilot.clean_prompts(["y" * 501])
    with pytest.raises(copilot.CopilotError, match="1024"):
        copilot.clean_synonyms(["z" * 1025], "Total Sales")
    problems = copilot.validate_schema_doc(
        {"$schema": copilot.SCHEMA_URL, "tables": [{"name": "Sales", "visibility": "Bogus"}]}
    )
    assert problems and "visibility" in problems[0]


def test_instructions_and_prompts() -> None:
    text = copilot.apply_set_instructions("", {"value": "Sales means [Total Sales].  \r\n\r\n"})
    assert text == "Sales means [Total Sales].\n"
    prompts = copilot.apply_set_example_prompts(
        "", {"value": ["Sales by category", "Sales by category", " Top 5 products "]}
    )
    data = json.loads(prompts)
    jsonschema.validate(data, official("examplePrompts"))
    assert data["prompts"] == ["Sales by category", "Top 5 products"]


def test_read_settings(tmp_path: Path) -> None:
    model = tmp_path / MODEL
    (model / "Copilot" / "Instructions").mkdir(parents=True)
    (model / copilot.SCHEMA_FILE).write_text(
        replay("", [syn("measure", "Total Sales", ["revenue"]), syn("table", "Sales", ["orders"])]),
        encoding="utf-8",
    )
    (model / copilot.INSTRUCTIONS_FILE).write_text("Fiscal year starts in April.\n", "utf-8")
    s = copilot.read_settings(model, "Retail")
    assert s.instructions == "Fiscal year starts in April.\n"
    assert s.synonyms == {
        "Retail/Sales/measure/Total Sales": ["revenue"],
        "Retail/Sales": ["orders"],
    }
    assert s.files == ["Copilot/Instructions/instructions.md", "Copilot/schema.json"]
    assert copilot.read_settings(tmp_path / "missing", "Retail").files == []


def test_change_set_creates_copilot_files_and_rollback_removes_them(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    head = gitops.head_sha(repo)
    svc = ChangeSetService(session, user)
    cs = svc.create_draft(conn, "Prep data for AI")
    svc.add_edit(cs, SCHEMA, syn("measure", "Total Sales", ["revenue"]))
    svc.add_edit(cs, INSTRUCTIONS, {"op": "copilot.set_instructions",
                                    "value": "Sales means [Total Sales]."})  # fmt: skip
    assert svc.propose(cs) == []
    assert all(i.before == "" for i in cs.items)
    svc.approve(cs)
    commit = svc.apply(cs)

    assert not (repo / SCHEMA).exists()  # working tree untouched
    assert gitops.out(repo, "status", "--porcelain") == "" and gitops.head_sha(repo) == head
    jsonschema.validate(json.loads(gitops.show(repo, commit, SCHEMA)), official("schema"))
    assert gitops.show(repo, commit, INSTRUCTIONS) == b"Sales means [Total Sales].\n"

    # A second pack stacks on the first and edits the file the first one created.
    more = svc.create_draft(conn, "More synonyms")
    svc.add_edit(more, SCHEMA, syn("table", "Customer", ["clients"], table="Customer"))
    assert svc.propose(more) == []
    svc.approve(more)
    second = svc.apply(more)
    tables = json.loads(gitops.show(repo, second, SCHEMA))["tables"]
    assert [t["name"] for t in tables] == ["Sales", "Customer"]

    svc.rollback(more)
    assert [
        t["name"] for t in json.loads(gitops.show(repo, more.branch or "", SCHEMA))["tables"]
    ] == ["Sales"]
    svc.rollback(cs)
    tip = cs.branch or ""
    assert gitops.blob_sha(repo, tip, SCHEMA) is None
    assert gitops.blob_sha(repo, tip, INSTRUCTIONS) is None


def test_created_file_that_appeared_meanwhile_is_a_conflict(
    session: Session, user: User, conn: Connection, repo: Path
) -> None:
    from datawright.core.changesets import ConflictError

    svc = ChangeSetService(session, user)
    cs = svc.create_draft(conn, "Prep data for AI")
    svc.add_edit(cs, INSTRUCTIONS, {"op": "copilot.set_instructions", "value": "Ours."})
    assert svc.propose(cs) == []
    svc.approve(cs)
    (repo / INSTRUCTIONS).parent.mkdir(parents=True)
    (repo / INSTRUCTIONS).write_text("Someone else's.\n", encoding="utf-8")
    gitops.git(repo, "add", "-A", as_datawright=False)
    gitops.git(repo, "commit", "-qm", "instructions written by hand", as_datawright=False)
    with pytest.raises(ConflictError):
        svc.apply(cs)


def test_offline_prep_run_proposes_a_valid_pack(
    session: Session, settings: Settings, user: User, conn: Connection, repo: Path
) -> None:
    run = run_prep(session, settings, ScriptedClient(offline_script(15)), conn, user)
    assert run.status == "succeeded", run.error
    cs = session.scalar(select(ChangeSet).where(ChangeSet.run_id == run.id))
    assert cs is not None and cs.status == "proposed" and "Copilot" in cs.title
    files = {i.file_path: i for i in cs.items}
    assert set(files) == {SCHEMA, INSTRUCTIONS, f"{MODEL}/{copilot.PROMPTS_FILE}"}
    schema = json.loads(files[SCHEMA].after)
    jsonschema.validate(schema, official("schema"))
    sales = next(t for t in schema["tables"] if t["name"] == "Sales")
    assert {"name": "Total Sales", "synonyms": ["sales"]} in sales["measures"]
    jsonschema.validate(json.loads(files[f"{MODEL}/{copilot.PROMPTS_FILE}"].after),
                        official("examplePrompts"))  # fmt: skip
    assert files[INSTRUCTIONS].after.startswith("<!-- Offline draft")
    assert not (repo / MODEL / "Copilot").exists()  # proposed only


def test_mcp_copilot_tools(
    settings: Settings, sessions: sessionmaker[Session], conn: Connection
) -> None:
    tools = DatawrightTools(settings, sessions)
    overview = tools.get_copilot_settings("test")[0]
    assert overview["model"] == "Retail" and overview["current"]["files"] == []
    assert any(m["measure"] == "Total Sales" for m in overview["measures"])
    tools.propose_synonyms("test", "Retail/Sales/measure/Total Sales", ["revenue", "turnover"])
    with pytest.raises(ValueError, match="already proposed"):
        tools.propose_synonyms("test", "Retail/Sales/measure/Total Sales", ["sales"])
    tools.propose_ai_instructions("test", "Retail", "Sales means [Total Sales].")
    with pytest.raises(ValueError, match="already proposed"):
        tools.propose_ai_instructions("test", "Retail", "Again.")
    tools.propose_example_prompts("test", "Retail", ["Total sales by category"])
    with pytest.raises(ValueError, match="unknown model"):
        tools.propose_ai_instructions("test", "Other", "x")
    tools.propose_description("test", "Retail/Sales/measure/Units ", "Units sold.")
    review = tools.submit_for_review("test")
    assert review["status"] == "proposed" and review["edits"] == 4
