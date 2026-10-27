"""A stand-in for the Claude client, for tests and `--offline` runs. It returns pre-written
responses shaped like the SDK's, so the real loop, tools, checks and change sets all run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

OFFLINE_MODEL = "scripted-offline"


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class Message:
    content: list[Block]
    stop_reason: str
    usage: Usage = field(default_factory=Usage)
    model: str = OFFLINE_MODEL
    stop_details: Any = None


def text(t: str) -> Message:
    return Message([Block("text", text=t)], "end_turn")


def tool_calls(*calls: tuple[str, dict[str, Any]]) -> Message:
    return Message(
        [
            Block("tool_use", id=f"toolu_{i}_{name}", name=name, input=args)
            for i, (name, args) in enumerate(calls)
        ],
        "tool_use",
    )


Script = Callable[[list[dict[str, Any]], dict[str, Any]], Message]


class ScriptedClient:
    """`script(messages, request_kwargs)` returns the next Message. `calls` keeps each request."""

    def __init__(self, script: Script | list[Message]) -> None:
        if isinstance(script, list):
            queue = list(script)

            def _from_list(_m: list[dict[str, Any]], _k: dict[str, Any]) -> Message:
                if not queue:
                    raise RuntimeError("scripted client ran out of responses")
                return queue.pop(0)

            self._script: Script = _from_list
        else:
            self._script = script
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> Message:
        self.calls.append({"messages": list(messages), **kwargs})
        return self._script(messages, kwargs)


def last_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last = messages[-1]
    if last["role"] != "user" or isinstance(last["content"], str):
        return []
    return [b for b in last["content"] if b.get("type") == "tool_result"]
