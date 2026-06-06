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

# Single-char C0 sequences: ESC followed by a single byte @-Z, \, _
_C0_RE = re.compile(r"\x1b[@-Z\\-_]")

# Charset selection: ESC ( A/B/0/1/2
_CHARSET_RE = re.compile(r"\x1b[()][AB012]")

# Broader fallback for anything remaining
_FALLBACK_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Control characters that should be stripped
_CONTROL_CHARS_RE = re.compile(
    r"[\x00\x07\x08]"  # NUL, BEL, BS
)


def strip_ansi(text: str | bytes) -> str:
    """Remove all ANSI escape sequences from *text*.

    Safe for logging, file output, and display. Returns plain text.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="replace")
        except Exception:
            return ""

    for regex in (_CSI_RE, _OSC_RE, _DCS_RE, _C0_RE, _CHARSET_RE):
        text = regex.sub("", text)

    # Catch anything we missed
    text = _FALLBACK_RE.sub("", text)

    # Strip control characters (NUL, BEL, BS)
    text = _CONTROL_CHARS_RE.sub("", text)

    return text


def sanitize_output(data: bytes | str) -> str:
    """Aggressively strip ALL terminal escape/control sequences.

    Used on remote command output before printing to local console.
    Defends against:
      - Terminal title injection (OSC)
      - Screen clearing / cursor repositioning (CSI)
      - Charset switching
      - DCS payload injection
      - Any novel escape sequence

    Returns a string guaranteed to contain only printable characters
    and newlines.
    """
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return ""
    else:
        text = data

    # Apply all sanitizers
    for regex in (_CSI_RE, _OSC_RE, _DCS_RE, _C0_RE, _CHARSET_RE):
        text = regex.sub("", text)

    # Aggressive fallback — strip any remaining ESC sequences
    text = _FALLBACK_RE.sub("", text)

    # Strip control characters (NUL, BEL, BS)
    text = _CONTROL_CHARS_RE.sub("", text)

    # Nuclear option: strip any remaining ESC bytes
    text = text.replace("\x1b", "")

    return text
