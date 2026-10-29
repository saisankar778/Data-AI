"""Edit operations a change set item can carry, keyed by intent `op`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from datawright.connectors.powerbi import copilot, tmdl

Apply = Callable[[str, dict[str, Any]], str]
Check = Callable[[str, str, list[dict[str, Any]]], list[str]]

OPS: dict[str, tuple[Apply, Check]] = {
    "tmdl.set_description": (tmdl.apply_set_description, tmdl.check_edits),
    "tmdl.set_property": (tmdl.apply_set_property, tmdl.check_edits),
    "tmdl.set_expression": (tmdl.apply_set_expression, tmdl.check_edits),
    "copilot.set_synonyms": (copilot.apply_set_synonyms, copilot.check_edits),
    "copilot.set_instructions": (copilot.apply_set_instructions, copilot.check_edits),
    "copilot.set_example_prompts": (copilot.apply_set_example_prompts, copilot.check_edits),
}

# Ops that may start from a file that doesn't exist yet (the item's `before` is "").
CREATES_FILES = frozenset(copilot.OPS)


def apply_intent(text: str, intent: dict[str, Any]) -> str:
    try:
        apply, _ = OPS[intent["op"]]
    except KeyError as e:
        raise ValueError(f"unknown edit op {intent.get('op')!r}") from e
    return apply(text, intent)


def replay(before: str, intents: list[dict[str, Any]]) -> str:
    """Apply every intent in order, starting from `before`."""
    text = before
    for intent in intents:
        text = apply_intent(text, intent)
    return text


def check_item(before: str, after: str, intents: list[dict[str, Any]]) -> list[str]:
    """Re-run every intent from `before` and require the exact same `after`, then the op checks."""
    problems: list[str] = []
    try:
        text = replay(before, intents)
    except (ValueError, KeyError) as e:
        return [f"could not replay edit: {e}"]
    if text != after:
        problems.append("stored result differs from replaying the edits")
    # Ops of one family share one structural check, which must see all its intents at once.
    checks: dict[Check, list[dict[str, Any]]] = {}
    for intent in intents:
        checks.setdefault(OPS[intent["op"]][1], []).append(intent)
    for check, group in checks.items():
        problems.extend(check(before, after, group))
    return problems
