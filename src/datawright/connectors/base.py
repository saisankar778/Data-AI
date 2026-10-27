"""Connector interface. Every connector declares the most it can do (read, propose, apply)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol

from datawright.core.capabilities import Capability


@dataclass
class AssetRecord:
    kind: str
    object_path: str
    name: str
    file_path: str | None = None  # relative to the connection root, forward slashes
    description: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Connector(Protocol):
    kind: ClassVar[str]
    max_capability: ClassVar[Capability]
    root: Path

    def check(self) -> list[str]:
        """Problems that stop this connection from working (empty list when healthy)."""
        ...

    def crawl(self) -> list[AssetRecord]:
        """Metadata only. Never table data."""
        ...
