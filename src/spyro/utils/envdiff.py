"""Redaction for ``.env`` text: key names are kept, values never leave this module."""

from __future__ import annotations

import re

# Anything left of the first `=` is the key, whatever it looks like (`MY-KEY=`, `export K=`,
# commented-out `#K=`): a line we cannot classify must not be printed with its value.
_ASSIGN = re.compile(r"^([^=\n]*=)(.*)$")


def _closes(s: str, quote: str) -> bool:
    """True if *s* contains an unescaped *quote*."""
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == quote:
            return True
        i += 1
    return False


def _entries(text: str) -> list[tuple[str, str, str]]:
    """``[(prefix, raw_value, kind), ...]`` per logical line; ``kind`` is ``"assign"`` or ``"line"``.

    A quoted value that does not close on its own line is folded with the
    following lines until an unescaped closing quote, so a multi-line private
    key or JSON blob is one entry. If it never closes, the rest of the file
    is part of it (fails closed).
    """
    lines = text.splitlines()
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        m = _ASSIGN.match(lines[i])
        if not m:
            out.append((lines[i], "", "line"))
            i += 1
            continue
        prefix, value = m.group(1), m.group(2)
        quote = value.lstrip()[:1]
        if quote in ("'", '"') and not _closes(value.lstrip()[1:], quote):
            while i + 1 < len(lines):
                i += 1
                value += "\n" + lines[i]
                if _closes(lines[i], quote):
                    break
        out.append((prefix, value, "assign"))
        i += 1
    return out


def redact_env(text: str, other: str | None = None, tag: str = "") -> list[str]:
    """*text* with every value replaced by ``***``.

    With *other*, a key whose value differs there is marked ``(tag)`` so a
    changed value still shows up in a diff without being revealed.
    """
    theirs = {p.strip(): v for p, v, kind in _entries(other) if kind == "assign"} if other else {}
    lines = []
    for prefix, value, kind in _entries(text):
        if kind == "line":
            lines.append(prefix)
            continue
        changed = prefix.strip() in theirs and theirs[prefix.strip()] != value
        lines.append(f"{prefix}***" + (f" ({tag})" if changed and tag else ""))
    return lines
