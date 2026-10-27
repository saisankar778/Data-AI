"""Tool registry. Every tool declares its capability; agents only see tools their run allows."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from datawright.core.capabilities import Capability


class ToolError(Exception):
    """A tool failed in a way the model should see and can recover from."""


@dataclass
class ToolResult:
    content: str
    summary: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    capability: Capability
    handler: Callable[[Any, dict[str, Any]], ToolResult]
    # Declared now, enforced once the first network-bound tool exists (all Phase 0 tools are local).
    timeout_s: float = 30.0
    cost_class: Literal["cheap", "expensive"] = "cheap"

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool names")
        for t in tools:
            if t.capability >= Capability.APPLY:
                raise ValueError(f"tool {t.name!r}: agents may never hold the apply capability")
        self.tools = tools

    def visible(self, permission: Capability) -> list[Tool]:
        return [t for t in self.tools if t.capability <= permission]


def customer_data(payload: Any) -> str:
    """Wrap text that came from the user's files. The model is told to treat it as data only."""
    body = (
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    )
    body = body.replace("</customer_data>", "<\\/customer_data>")
    return f"<customer_data>\n{body}\n</customer_data>"


def unwrap_customer_data(content: str) -> str:
    start, end = content.find("<customer_data>\n"), content.rfind("\n</customer_data>")
    if start < 0 or end < 0:
        return content
    return content[start + len("<customer_data>\n") : end]
