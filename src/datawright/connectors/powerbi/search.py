"""Keyword search over a model's metadata (names, descriptions, DAX), for `ask` and MCP."""

from __future__ import annotations

import re
from typing import Any

from datawright.connectors.powerbi.pbip import ObjectRef, PowerBIConnector

STOP = set(
    "the and for what which where how does is are of in a an to do we our with show me by this "
    "that from measure measures column columns table tables model value get".split()
)


def terms(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9%]+", query.lower())
    out = []
    for w in words:
        if w in STOP or len(w) < 2:
            continue
        out.append(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return list(dict.fromkeys(out))


def _fields(o: ObjectRef) -> list[tuple[str, float]]:
    expr = o.measure.expression if o.measure else (o.column.expression or "") if o.column else ""
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", o.name).lower()  # UnitPrice -> unit price
    return [
        (name, 3.0),
        ((o.description or "").lower(), 2.0),
        (o.table.name.lower(), 1.0),
        (expr.lower(), 0.5),
    ]


def search(connector: PowerBIConnector, query: str, limit: int = 10) -> list[dict[str, Any]]:
    words = terms(query)
    if not words:
        return []
    hits = []
    for o in connector.objects():
        if o.table.is_auto_date:
            continue
        fields = _fields(o)
        score = sum(weight for w in words for text, weight in fields if w in text)
        if score > 0:
            hits.append((score + (0.5 if o.kind == "measure" else 0.0), o))
    hits.sort(key=lambda h: (-h[0], h[1].object_path))
    return [
        {
            "object_path": o.object_path,
            "kind": o.kind,
            "table": o.table.name,
            "name": o.name,
            "description": (o.description or "")[:240],
            "hidden": o.is_hidden,
            "score": round(s, 1),
        }
        for s, o in hits[:limit]
    ]
