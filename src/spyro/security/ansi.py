"""ANSI escape sequence sanitization — security boundary.

All remote output passes through these functions before reaching
the local terminal. This prevents terminal reprogramming attacks.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# CSI sequences: ESC [ ... final byte
# ---------------------------------------------------------------------------

_CSI_RE = re.compile(
    r"""
    \x1b        # ESC
    \[              # CSI introducer
    [0-?]*          # parameter bytes
    [ -/]*          # intermediate bytes
    [@-~]           # final byte
    """,
    re.VERBOSE | re.DOTALL,
)

# OSC sequences: ESC ] ... (ST | BEL)
_OSC_RE = re.compile(
    r"\x1b\].*?(?:\x07|\x1b\\)",
    re.DOTALL,
)

# DCS sequences: ESC P ... ESC \
_DCS_RE = re.compile(
    r"\x1bP.*?\x1b\\",
    re.DOTALL,
)

# Any other escape sequence: ESC, optional intermediate bytes, one final byte.
# Covers charset selection (ESC ( B), terminal reset (ESC c), save/restore
# cursor (ESC 7 / ESC 8), keypad modes (ESC = / ESC >) and the two-byte C1 forms.
_ESC_RE = re.compile(r"\x1b[ -/]*[0-~]")

# Control characters that should be stripped: NUL, BEL, BS, VT, FF, DEL and the
# 8-bit C1 range (U+0080-U+009F, which includes single-character CSI/OSC/DCS
# introducers). CR is handled separately (see _resolve_carriage_returns).
_CONTROL_CHARS_RE = re.compile(r"[\x00\x07\x08\x0b\x0c\x7f\x80-\x9f]")


def _resolve_carriage_returns(text: str) -> str:
    """Apply ``\\r`` the way a terminal would, so no raw CR ever reaches one.

    CRLF becomes LF, and within a line only the last ``\\r``-separated frame
    survives: a progress bar (scp, composer) shows its final state instead of
    every frame glued together, and remote text can no longer overwrite the
    start of the line spyro printed (e.g. its ``[profile]`` prefix).
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        frames = [f for f in line.split("\r") if f]
        lines.append(frames[-1] if frames else "")
    return "\n".join(lines)


def strip_ansi(text: str | bytes) -> str:
    """Remove every terminal escape/control sequence from *text*.

    Used on remote output before it is printed or matched. Defends against:
      - Terminal title / clipboard injection (OSC)
      - Screen clearing / cursor repositioning (CSI)
      - Charset switching, DCS payloads
      - Terminal reset and save/restore (ESC c, ESC 7, ESC 8, ESC =)
      - Carriage-return overwriting (resolved to the last frame) and PTY CRLF line endings

    Returns a string containing only printable characters, tabs and newlines.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    for regex in (_CSI_RE, _OSC_RE, _DCS_RE, _ESC_RE):
        text = regex.sub("", text)

    text = _CONTROL_CHARS_RE.sub("", text)
    text = _resolve_carriage_returns(text)

    # Anything still starting with ESC is an unknown sequence: drop the byte.
    return text.replace("\x1b", "")


# Kept for callers that want the intent spelled out at the call site.
sanitize_output = strip_ansi
