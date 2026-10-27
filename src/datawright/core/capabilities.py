from __future__ import annotations

from enum import IntEnum


class Capability(IntEnum):
    READ = 1
    PROPOSE = 2
    APPLY = 3

    @classmethod
    def parse(cls, value: str) -> Capability:
        try:
            return cls[value.strip().upper()]
        except KeyError as e:
            raise ValueError(f"unknown capability {value!r} (use read, propose or apply)") from e

    @property
    def label(self) -> str:
        return self.name.lower()


ROLES = ("viewer", "analyst", "developer", "admin")
APPROVER_ROLES = frozenset({"developer", "admin"})
