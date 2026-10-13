"""Lossless ``.env`` editing: ``serialize(parse(t)) == t`` byte for byte.

Values are encoded for phpdotenv v5 (``Parser/EntryParser.php``): unquoted values
end at whitespace/``#`` and interpolate ``$``; single quotes are fully literal;
double quotes accept only ``\\" \\\\ \\$ \\f \\n \\r \\t \\v``.
"""

from __future__ import annotations

import base64
import re
import secrets
import string
from dataclasses import dataclass

from .envdiff import _closes

_ASSIGN = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_.]*)(\s*=\s*)(.*)$", re.S)
# A commented-out assignment (`# KEY=`, `#export KEY=`); `## KEY` and prose like `# set KEY= to` are not
_HIDDEN = re.compile(r"^\s*#[ \t]*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=(.*)$", re.S)
_UNCOMMENT = re.compile(r"^(\s*)# ?")
_BARE = re.compile(r"[A-Za-z0-9_./:@,+=%-]+")
_UNQUOTED_COMMENT = re.compile(r"\s+#.*$", re.S)


@dataclass
class Entry:
    raw: str  # exact text, line ending(s) included
    line: int  # 1-based line number of the first line
    key: str | None = None  # None for blank lines, comments and unparseable lines
    error: str = ""
    hidden: str | None = None  # KEY of a commented-out assignment (all its lines are commented)


def _eol(raw: str) -> str:
    return raw[len(raw.rstrip("\r\n")):]


def parse(text: str) -> list[Entry]:
    lines = text.splitlines(keepends=True)
    out: list[Entry] = []
    i = 0
    while i < len(lines):
        start, raw = i, lines[i]
        body = raw.rstrip("\r\n")
        m = _ASSIGN.match(body)
        if not m:
            stripped = body.strip()
            err = "" if not stripped or stripped.startswith("#") else "not KEY=VALUE"
            h = _HIDDEN.match(body)
            quote = h.group(2).strip()[:1] if h else ""
            if quote in ("'", '"') and not _closes(h.group(2).strip()[1:], quote):
                # a hidden multi-line value: fold the following commented lines up to the closing quote
                j, more = i, ""
                while j + 1 < len(lines) and lines[j + 1].lstrip().startswith("#"):
                    j += 1
                    more += lines[j]
                    if _closes(_UNCOMMENT.sub("", lines[j], 1), quote):
                        raw += more
                        i = j
                        break
            out.append(Entry(raw, start + 1, None, err, h.group(1) if h else None))
            i += 1
            continue
        value = m.group(4)
        quote = value[:1]
        err = ""
        if quote in ("'", '"') and not _closes(value[1:], quote):
            while True:
                if i + 1 >= len(lines):
                    err = f"unterminated {quote} quote"
                    break
                i += 1
                raw += lines[i]
                if _closes(lines[i], quote):
                    break
        out.append(Entry(raw, start + 1, m.group(2), err))
        i += 1
    return out


def serialize(doc: list[Entry]) -> str:
    return "".join(e.raw for e in doc)


def problems(doc: list[Entry]) -> list[str]:
    return [f"line {e.line}: {e.error}" for e in doc if e.error]


def find(doc: list[Entry], key: str) -> list[int]:
    return [i for i, e in enumerate(doc) if e.key == key]


def find_hidden(doc: list[Entry], key: str) -> list[int]:
    return [i for i, e in enumerate(doc) if e.hidden == key]


def raw_value(e: Entry) -> str:
    body = e.raw.rstrip("\r\n")
    return _ASSIGN.match(body).group(4)


def decode(raw: str) -> str:
    """The value phpdotenv reads from a raw value part (the forms :func:`encode` writes, plus plain ones)."""
    raw = raw.strip()
    if raw[:1] == "'" and raw.rfind("'") > 0:
        return raw[1:raw.rfind("'")]
    if raw[:1] == '"':
        out, i = [], 1
        esc = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v"}
        while i < len(raw) and raw[i] != '"':
            if raw[i] == "\\" and i + 1 < len(raw):
                out.append(esc.get(raw[i + 1], raw[i + 1]))
                i += 2
            else:
                out.append(raw[i])
                i += 1
        return "".join(out)
    return "" if raw.startswith("#") else _UNQUOTED_COMMENT.sub("", raw)


def encode(v: str) -> str:
    if v == "":
        return ""
    if _BARE.fullmatch(v):
        return v
    if "'" not in v and "\n" not in v and "\r" not in v:
        return f"'{v}'"
    for a, b in (("\\", "\\\\"), ('"', '\\"'), ("$", "\\$"), ("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t")):
        v = v.replace(a, b)
    return f'"{v}"'


def _newline(doc: list[Entry]) -> str:
    return "\r\n" if any(e.raw.endswith("\r\n") for e in doc) else "\n"


def set_value(doc: list[Entry], idx: int, encoded: str) -> None:
    """Rewrite one entry's value, keeping ``export``, key spelling, spacing, position and line ending."""
    e = doc[idx]
    first = e.raw.splitlines(keepends=True)[0].rstrip("\r\n")
    m = _ASSIGN.match(first)
    old = m.group(4)
    comment = ""
    if old[:1] not in ("'", '"'):
        c = _UNQUOTED_COMMENT.search(old)
        comment = c.group(0) if c else ""
    doc[idx] = Entry(f"{m.group(1)}{m.group(2)}{m.group(3)}{encoded}{comment}{_eol(e.raw)}", e.line, e.key)


def insert(doc: list[Entry], key: str, encoded: str, after: str | None = None) -> None:
    """New ``KEY=value`` after *after*, else after the last commented-out ``# KEY=``, else at the end."""
    nl = _newline(doc)
    entry = Entry(f"{key}={encoded}{nl}", 0, key)
    pos = None
    if after:
        hits = find(doc, after)
        if not hits:
            raise KeyError(after)
        pos = hits[-1] + 1
    elif hidden := find_hidden(doc, key):
        pos = hidden[-1] + 1
    if pos is None:
        pos = len(doc)
    if pos > 0 and not _eol(doc[pos - 1].raw):
        doc[pos - 1].raw += nl
    doc.insert(pos, entry)


def remove(doc: list[Entry], idx: int) -> None:
    del doc[idx]


def hide(doc: list[Entry], idx: int) -> None:
    e = doc[idx]
    doc[idx] = Entry("".join("# " + l for l in e.raw.splitlines(keepends=True)), e.line, hidden=e.key)


def unhide(doc: list[Entry], idx: int) -> None:
    e = doc[idx]
    doc[idx] = Entry("".join(_UNCOMMENT.sub(r"\1", l, 1) for l in e.raw.splitlines(keepends=True)), e.line, e.hidden)


def generate(spec: str) -> str:
    """``laravel-key`` or ``N[:hex|base64|alnum]`` (N random bytes; N characters for alnum)."""
    if spec == "laravel-key":
        return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    n, _, kind = spec.partition(":")
    if not n.isdigit() or int(n) < 8:
        raise ValueError("N must be a number >= 8")
    n_ = int(n)
    if kind in ("", "hex"):
        return secrets.token_hex(n_)
    if kind == "base64":
        return base64.b64encode(secrets.token_bytes(n_)).decode()
    if kind == "alnum":
        return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n_))
    raise ValueError(f"unknown format {kind!r} (hex, base64, alnum)")
