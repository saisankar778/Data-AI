"""Object paths: `Model/Table/measure/Name`. A `/` or `\\` inside a name is escaped with `\\`."""

from __future__ import annotations


def _esc(segment: str) -> str:
    return segment.replace("\\", "\\\\").replace("/", "\\/")


def make_path(*segments: str) -> str:
    return "/".join(_esc(s) for s in segments)


def split_path(path: str) -> list[str]:
    parts, cur, i = [], [], 0
    while i < len(path):
        ch = path[i]
        if ch == "\\" and i + 1 < len(path):
            cur.append(path[i + 1])
            i += 2
            continue
        if ch == "/":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts
