"""Phase 4 Security Test: Malicious ANSI escape sequences.

Creates a mock server that sends various terminal escape sequences
and verifies Spyro strips them all correctly.

Attack vectors tested:
  1. OSC title injection (set terminal title to malicious URL)
  2. CSI cursor repositioning (overwrite previous output)
  3. DCS payload injection (terminal reprogramming)
  4. Charset switching (alter character display)
  5. Combined attack (multiple vectors in one payload)
  6. Null bytes and control characters
"""

from __future__ import annotations

import os
import sys

from spyro.security.ansi import strip_ansi, sanitize_output


# ---------------------------------------------------------------------------
# Attack payloads
# ---------------------------------------------------------------------------

ATTACKS = [
    (
        "OSC title injection",
        b"normal output\x1b]0;https://evil.com/malware\x07more output",
        "normal outputmore output",
    ),
    (
        "CSI cursor repositioning",
        b"real data\x1b[2J\x1b[HPHANTOM DATA",
        "real dataPHANTOM DATA",
    ),
    (
        "DCS payload injection",
        b"before\x1bPq|t3|k3r\x1b\\after",
        "beforeafter",
    ),
    (
        "Charset switching",
        b"ascii\x1b(Aline1\x1b(Bline2",
        "asciiline1line2",
    ),
    (
        "CSI color + style injection",
        b"\x1b[1;31;44mHIDDEN\x1b[0m VISIBLE",
        "HIDDEN VISIBLE",
    ),
    (
        "Nested/stacked escapes",
        b"\x1b[31m\x1b[1m\x1b[4mDEEP\x1b[0m",
        "DEEP",
    ),
    (
        "OSC with BEL terminator",
        b"\x1b]8;;https://evil.com\x1b\\click here\x1b]8;;\x1b\\",
        "click here",
    ),
    (
        "OSC with ST terminator",
        b"\x1b]0;Title\x1b\\",
        "",
    ),
    (
        "CSI erase display",
        b"visible\x1b[2Jinvisible",
        "visibleinvisible",
    ),
    (
        "CSI scroll region",
        b"top\x1b[rbot",
        "topbot",
    ),
    (
        "Mouse tracking enable",
        b"\x1b[?1000h\x1b[?1006hactual output",
        "actual output",
    ),
    (
        "Alternate screen buffer",
        b"\x1b[?1049h\x1b[?1049ldata",
        "data",
    ),
    (
        "Device status request",
        b"before\x1b[5nafter",
        "beforeafter",
    ),
    (
        "OSC hyperlink injection",
        b"\x1b]8;;https://evil.com\x1b\\Click here\x1b]8;;\x1b\\",
        "Click here",
    ),
    (
        "Sixel graphics injection",
        b"\x1bPq#1;2;0;0;0#2;1;0;0;0~1\x1b\\text",
        "text",
    ),
    (
        "Mixed real + attack",
        b"line1\n\x1b[31mHIDDEN\x1b[0m\nline3",
        "line1\nHIDDEN\nline3",
    ),
    (
        "Null bytes",
        b"data\x00\x00\x00more",
        "datamore",
    ),
    (
        "Backspace abuse",
        b"abc\x08\x08\x08XYZ",
        "abcXYZ",
    ),
    (
        "Tab injection",
        b"before\x1bHafter",
        "beforeafter",
    ),
    (
        "Ring buffer (BEL) in CSI",
        b"\x07\x07\x07output",
        "output",
    ),
    (
        "Terminal reset (ESC c)",
        b"before\x1bcafter",
        "beforeafter",
    ),
    (
        "Save/restore cursor + keypad mode",
        b"a\x1b7b\x1b8c\x1b=d\x1b>e",
        "abcde",
    ),
    (
        "Carriage-return line spoofing",
        b"Permission denied\rLogin OK",
        "Login OK",
    ),
    (
        "8-bit C1 CSI introducer",
        "red\u009b31mtext".encode("utf-8"),
        "red31mtext",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("name,payload,expected", ATTACKS, ids=[a[0] for a in ATTACKS])
def test_attack_payload_is_neutralised(name, payload, expected):
    """Every payload loses all escape/control bytes but keeps its content."""
    for fn in (sanitize_output, strip_ansi):
        result = fn(payload)
        assert "\x1b" not in result, f"ESC remains: {result!r}"
        assert "\x07" not in result, f"BEL remains: {result!r}"
        assert "\r" not in result, f"CR remains: {result!r}"
        assert not any("\x80" <= c <= "\x9f" for c in result), f"C1 remains: {result!r}"
        assert expected in result, f"expected {expected!r}, got {result!r}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hello world", "hello world"),
        ("line1\nline2\nline3", "line1\nline2\nline3"),
        ("col1\tcol2", "col1\tcol2"),
        ("café résumé", "café résumé"),
        ("port 3306", "port 3306"),
        ("", ""),
        ("\x1b[31m\x1b[0m", ""),
        ("CRLF line\r\n", "CRLF line\n"),
        ("big.bin 10%\rbig.bin 50%\rbig.bin 100%\n", "big.bin 100%\n"),
        ("ends with cr\r", "ends with cr"),
        ("[prof] ok\r[prof] SPOOF", "[prof] SPOOF"),
    ],
)
def test_strip_ansi_preserves_content(text, expected):
    assert strip_ansi(text) == expected


def test_bytes_input():
    """Raw PTY bytes are decoded and sanitized."""
    result = sanitize_output(b"\x1b[31mERROR\x1b[0m: connection refused\n")
    assert result == "ERROR: connection refused\n"


def main():
    """Script mode: python tests/security/test_ansi_attacks.py"""
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))


if __name__ == "__main__":
    main()
