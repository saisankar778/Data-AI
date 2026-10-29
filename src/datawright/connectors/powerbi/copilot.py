"""Copilot "Prep data for AI" files in a PBIP model (docs/specs/powerbi.md, "Copilot pack").

Layout and limits come from Microsoft's published schemas (microsoft/json-schemas,
fabric/item/semanticModel/copilot/*) and from Power BI Desktop 2.159's packaging code:

    <Model>.SemanticModel/Copilot/schema.json              AI data schema, incl. synonyms
    <Model>.SemanticModel/Copilot/examplePrompts.json
    <Model>.SemanticModel/Copilot/Instructions/instructions.md

Edits here are change-set intents like the TMDL ones: `apply_*` turns text into text and
`check_edits` proves that only the targeted entity changed.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/copilot"
SCHEMA_URL = f"{SCHEMA_BASE}/schema/1.0.0/schema.json"
PROMPTS_URL = f"{SCHEMA_BASE}/examplePrompts/1.0.0/schema.json"

FOLDER = "Copilot"
SCHEMA_FILE = f"{FOLDER}/schema.json"
PROMPTS_FILE = f"{FOLDER}/examplePrompts.json"
INSTRUCTIONS_FILE = f"{FOLDER}/Instructions/instructions.md"

# Limits from Desktop's Microsoft.PowerBI.Packaging.Shared.Copilot constants.
MAX_INSTRUCTIONS = 15000
MAX_NAME = 512
MAX_SYNONYMS = 1024
MAX_SYNONYM = 1024
MAX_PROMPTS = 10000
MAX_PROMPT = 500
VISIBILITY = ("Auto", "Visible", "Hidden")
ENTITY_LISTS = {"column": "columns", "measure": "measures"}
FILE_KINDS = {
    "copilot.set_synonyms": "schema",
    "copilot.set_instructions": "instructions",
    "copilot.set_example_prompts": "prompts",
}
OPS = tuple(FILE_KINDS)


class CopilotError(ValueError):
    pass


@dataclass
class CopilotSettings:
    """What a model's Copilot folder holds today (all empty when there is no folder)."""

    instructions: str = ""
    example_prompts: list[str] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)  # object path -> synonyms
    files: list[str] = field(default_factory=list)  # relative to the model folder


def model_folder(definition_dir: Path) -> Path:
    """`<Model>.SemanticModel` for its `definition` folder."""
    return definition_dir.parent


def read_settings(model_dir: Path, model_name: str) -> CopilotSettings:
    from datawright.core.paths import make_path

    out = CopilotSettings()
    root = model_dir / FOLDER
    if not root.is_dir():
        return out
    out.files = sorted(p.relative_to(model_dir).as_posix() for p in root.rglob("*") if p.is_file())
    if (model_dir / INSTRUCTIONS_FILE).is_file():
        out.instructions = (model_dir / INSTRUCTIONS_FILE).read_text(encoding="utf-8-sig")
    if (model_dir / PROMPTS_FILE).is_file():
        prompts = _load(_read(model_dir / PROMPTS_FILE)).get("prompts") or []
        out.example_prompts = [p for p in prompts if isinstance(p, str)]
    if (model_dir / SCHEMA_FILE).is_file():
        for t in _load(_read(model_dir / SCHEMA_FILE)).get("tables") or []:
            if not isinstance(t, dict) or "name" not in t:
                continue
            if t.get("synonyms"):
                out.synonyms[make_path(model_name, t["name"])] = list(t["synonyms"])
            for kind, key in ENTITY_LISTS.items():
                for e in t.get(key) or []:
                    if isinstance(e, dict) and e.get("synonyms") and "name" in e:
                        path = make_path(model_name, t["name"], kind, e["name"])
                        out.synonyms[path] = list(e["synonyms"])
    return out


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _load(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CopilotError(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise CopilotError("expected a JSON object")
    return data


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def clean_synonyms(values: list[str], name: str) -> list[str]:
    """Trim, drop blanks, the object's own name and duplicates (case-insensitive); keep order."""
    seen = {name.casefold()}
    out = []
    for v in values:
        s = " ".join(str(v).split())
        if not s or s.casefold() in seen:
            continue
        if len(s) > MAX_SYNONYM:
            raise CopilotError(f"synonym is {len(s)} characters; the limit is {MAX_SYNONYM}")
        seen.add(s.casefold())
        out.append(s)
    if len(out) > MAX_SYNONYMS:
        raise CopilotError(f"{len(out)} synonyms; the limit is {MAX_SYNONYMS}")
    return out


def _entity(data: dict[str, Any], intent: dict[str, Any], create: bool) -> dict[str, Any] | None:
    kind, name = intent["kind"], intent["name"]
    table_name = name if kind == "table" else intent["table"]
    tables = data.setdefault("tables", []) if create else (data.get("tables") or [])
    table = next((t for t in tables if isinstance(t, dict) and t.get("name") == table_name), None)
    if table is None:
        if not create:
            return None
        table = {"name": table_name}
        tables.append(table)
    if kind == "table":
        return table
    key = ENTITY_LISTS[kind]
    entries = table.setdefault(key, []) if create else (table.get(key) or [])
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None and create:
        entry = {"name": name}
        entries.append(entry)
    return entry


def _set_synonyms(data: dict[str, Any], intent: dict[str, Any]) -> None:
    if intent.get("kind") not in ("table", *ENTITY_LISTS):
        raise CopilotError(f"kind must be table, column or measure, not {intent.get('kind')!r}")
    synonyms = clean_synonyms(list(intent.get("synonyms") or []), intent["name"])
    if not synonyms:
        entity = _entity(data, intent, create=False)
        if entity is not None:
            entity.pop("synonyms", None)
        return
    entity = _entity(data, intent, create=True)
    assert entity is not None
    entity["synonyms"] = synonyms


def apply_set_synonyms(text: str, intent: dict[str, Any]) -> str:
    data = _load(text)
    data.setdefault("$schema", SCHEMA_URL)
    _set_synonyms(data, intent)
    return _dump(data)


def clean_instructions(value: str) -> str:
    text = "\n".join(line.rstrip() for line in str(value).replace("\r\n", "\n").split("\n"))
    text = text.strip("\n")
    if not text.strip():
        raise CopilotError("instructions are empty")
    if len(text) > MAX_INSTRUCTIONS:
        raise CopilotError(
            f"instructions are {len(text)} characters; the limit is {MAX_INSTRUCTIONS}"
        )
    return text + "\n"


def apply_set_instructions(text: str, intent: dict[str, Any]) -> str:
    return clean_instructions(intent["value"])


def clean_prompts(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        s = " ".join(str(v).split())
        if not s or s in out:
            continue
        if len(s) > MAX_PROMPT:
            raise CopilotError(f"example prompt is {len(s)} characters; the limit is {MAX_PROMPT}")
        out.append(s)
    if not out:
        raise CopilotError("no example prompts")
    if len(out) > MAX_PROMPTS:
        raise CopilotError(f"{len(out)} example prompts; the limit is {MAX_PROMPTS}")
    return out


def apply_set_example_prompts(text: str, intent: dict[str, Any]) -> str:
    data = _load(text)
    data.setdefault("$schema", PROMPTS_URL)
    data["prompts"] = clean_prompts(list(intent["value"]))
    return _dump(data)


# ---------------------------------------------------------------- validation


def validate_schema_doc(data: dict[str, Any]) -> list[str]:
    """The published schema's rules that matter for what Datawright writes."""
    problems = []
    if not isinstance(data.get("$schema"), str):
        problems.append("missing $schema")
    tables = data.get("tables")
    if tables is not None and not isinstance(tables, list):
        return problems + ["tables must be a list"]

    def entity(e: Any, where: str) -> None:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            problems.append(f"{where}: every entry needs a name")
            return
        if len(e["name"]) > MAX_NAME:
            problems.append(f"{where}: name longer than {MAX_NAME}")
        if "visibility" in e and e["visibility"] not in VISIBILITY:
            problems.append(f"{where} {e['name']!r}: visibility must be one of {VISIBILITY}")
        syn = e.get("synonyms")
        if syn is not None:
            if not isinstance(syn, list) or len(syn) > MAX_SYNONYMS:
                problems.append(f"{where} {e['name']!r}: synonyms: a list of ≤{MAX_SYNONYMS}")
            elif any(not isinstance(s, str) or len(s) > MAX_SYNONYM for s in syn):
                problems.append(f"{where} {e['name']!r}: each synonym ≤{MAX_SYNONYM} characters")

    for t in tables or []:
        entity(t, "table")
        if isinstance(t, dict):
            for key in ("columns", "measures", "hierarchies"):
                for e in t.get(key) or []:
                    entity(e, key[:-1])
    return problems


def _expected(before: str, file_kind: str, intents: list[dict[str, Any]]) -> Any:
    """What `after` must parse to: `before` parsed, with the intents applied structurally."""
    if file_kind == "instructions":
        return clean_instructions(intents[-1]["value"])
    data = copy.deepcopy(_load(before))
    if file_kind == "schema":
        data.setdefault("$schema", SCHEMA_URL)
        for intent in intents:
            _set_synonyms(data, intent)
    else:
        data.setdefault("$schema", PROMPTS_URL)
        data["prompts"] = clean_prompts(list(intents[-1]["value"]))
    return data


def check_edits(before: str, after: str, intents: list[dict[str, Any]]) -> list[str]:
    ops = {str(i.get("op")) for i in intents}
    if len(ops) != 1:
        return ["one Copilot file can't mix edit kinds"]
    file_kind = FILE_KINDS[ops.pop()]
    try:
        expected = _expected(before, file_kind, intents)
        actual: Any = after if file_kind == "instructions" else _load(after)
    except (CopilotError, KeyError) as e:
        return [str(e)]
    problems = [] if actual == expected else ["the file changed beyond the requested edits"]
    if file_kind == "schema":
        problems += validate_schema_doc(actual)
    if file_kind != "instructions" and not after.endswith("\n"):
        problems.append("file must end with a newline")
    return problems
