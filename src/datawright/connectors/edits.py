"""Edit operations a change set item can carry, keyed by intent `op`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from datawright.connectors.powerbi import tmdl

Apply = Callable[[str, dict[str, Any]], str]
Check = Callable[[str, str, list[dict[str, Any]]], list[str]]

OPS: dict[str, tuple[Apply, Check]] = {
    "tmdl.set_description": (tmdl.apply_set_description, tmdl.check_set_description),
}


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
    for op in {i["op"] for i in intents}:
        _, check = OPS[op]
        problems.extend(check(before, after, [i for i in intents if i["op"] == op]))
    return problems
