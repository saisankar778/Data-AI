"""Small DAX scanner: find references and operators while ignoring strings and comments.

Not a parser. It knows enough lexical structure (strings, 'quoted tables', [brackets],
comments) to keep rules from matching inside text, and to tell `Table[Column]` from `[Measure]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Ref:
    table: str | None  # None for an unqualified reference like [Total Sales]
    name: str


def _read_quoted(dax: str, i: int, quote: str) -> tuple[str, int]:
    """Read a 'quoted' or "quoted" token starting at dax[i]; return (content, index after)."""
    out, j, n = [], i + 1, len(dax)
    while j < n:
        if dax[j] == quote:
            if j + 1 < n and dax[j + 1] == quote:
                out.append(quote)
                j += 2
                continue
            return "".join(out), j + 1
        out.append(dax[j])
        j += 1
    return "".join(out), n


def _read_bracket(dax: str, i: int) -> tuple[str, int]:
    """Read [name] starting at dax[i]; `]]` inside the name is a literal `]`."""
    out, j, n = [], i + 1, len(dax)
    while j < n:
        if dax[j] == "]":
            if j + 1 < n and dax[j + 1] == "]":
                out.append("]")
                j += 2
                continue
            return "".join(out), j + 1
        out.append(dax[j])
        j += 1
    return "".join(out), n


def _skip_comment(dax: str, i: int) -> int | None:
    """If a comment starts at i, return the index after it."""
    two = dax[i : i + 2]
    if two in ("//", "--"):
        end = dax.find("\n", i)
        return len(dax) if end < 0 else end
    if two == "/*":
        end = dax.find("*/", i + 2)
        return len(dax) if end < 0 else end + 2
    return None


def code_only(dax: str) -> str:
    """The expression with strings and comments blanked out (same length, so offsets hold)."""
    out = list(dax)
    i, n = 0, len(dax)
    while i < n:
        skip = _skip_comment(dax, i)
        if skip is not None:
            for k in range(i, skip):
                if out[k] != "\n":
                    out[k] = " "
            i = skip
            continue
        ch = dax[i]
        if ch == '"':
            _, end = _read_quoted(dax, i, '"')
            for k in range(i, end):
                out[k] = " "
            i = end
        elif ch == "'":
            _, i = _read_quoted(dax, i, "'")
        elif ch == "[":
            _, i = _read_bracket(dax, i)
        else:
            i += 1
    return "".join(out)


def references(dax: str) -> list[Ref]:
    refs: list[Ref] = []
    i, n = 0, len(dax)
    while i < n:
        skip = _skip_comment(dax, i)
        if skip is not None:
            i = skip
            continue
        ch = dax[i]
        if ch == '"':
            _, i = _read_quoted(dax, i, '"')
        elif ch == "'":
            table, j = _read_quoted(dax, i, "'")
            k = j
            while k < n and dax[k] in " \t":
                k += 1
            if k < n and dax[k] == "[":
                name, i = _read_bracket(dax, k)
                refs.append(Ref(table, name))
            else:
                i = j
        elif ch == "[":
            name, i = _read_bracket(dax, i)
            refs.append(Ref(None, name))
        else:
            m = _IDENT.match(dax, i)
            if m and (i == 0 or not (dax[i - 1].isalnum() or dax[i - 1] in "_.")):
                end = m.end()
                if end < n and dax[end] == "[":
                    name, i = _read_bracket(dax, end)
                    refs.append(Ref(m.group(0), name))
                    continue
                i = end
            else:
                i += 1
    return refs


def masked(dax: str) -> str:
    """Same length as `dax`, with strings, comments and the insides of names blanked out, so
    what remains is operators, function names, parentheses and literals."""
    return code_only(_blank_names(dax))


def has_division_operator(dax: str) -> bool:
    """True when `/` is used as an operator: not in strings, [names], 'tables' or comments."""
    return "/" in masked(dax)


def simple_division(dax: str) -> tuple[str, str] | None:
    """(numerator, denominator) when the whole expression is exactly `A / B` with no other
    top-level operator; None otherwise (rewriting anything more complex needs review by a person
    or a model, not a regex)."""
    code = masked(dax)
    depth, slashes = 0, []
    for i, ch in enumerate(code):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch in "+-*/&|<>=^,":
            if ch != "/":
                return None
            slashes.append(i)
    if depth != 0 or len(slashes) != 1:
        return None
    lhs, rhs = dax[: slashes[0]].strip(), dax[slashes[0] + 1 :].strip()
    if not lhs or not rhs or re.match(r"(?i)VAR\s", lhs):
        return None
    return lhs, rhs


def _blank_names(dax: str) -> str:
    """Blank out the inside of [names] and 'tables' so a `/` in a name isn't seen as code."""
    out = list(dax)
    i, n = 0, len(dax)
    while i < n:
        skip = _skip_comment(dax, i)
        if skip is not None:
            i = skip
            continue
        ch = dax[i]
        if ch == '"':
            _, i = _read_quoted(dax, i, '"')
        elif ch in "'[":
            _, end = _read_quoted(dax, i, "'") if ch == "'" else _read_bracket(dax, i)
            for k in range(i + 1, max(i + 1, end - 1)):
                out[k] = " "
            i = end
        else:
            i += 1
    return "".join(out)


def calls(dax: str, function: str) -> bool:
    """True when `function(` appears in code (case-insensitive), not in strings or comments."""
    return re.search(rf"(?<![\w.]){re.escape(function)}\s*\(", code_only(dax), re.I) is not None


def normalized(dax: str) -> str:
    """Expression with all whitespace removed (BPA's duplicate-measure comparison)."""
    return re.sub(r"\s+", "", dax)
