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
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_attacks():
    """Run all attack payloads through sanitization."""
    print("=" * 60)
    print("Malicious ANSI Escape Sequence Tests")
    print("=" * 60)

    passed = 0
    failed = 0

    for name, payload, expected in ATTACKS:
        result = sanitize_output(payload)

        # Normalize: strip ANSI, check no ESC bytes remain
        has_esc = "\x1b" in result
        has_bell = "\x07" in result

        # Check expected content is preserved
        content_ok = expected in result if expected else True

        # Check no escape sequences remain
        clean = not has_esc and not has_bell

        if clean and content_ok:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}")
            if has_esc:
                print(f"         ESC bytes remain: {result!r}")
            if has_bell:
                print(f"         BEL bytes remain: {result!r}")
            if not content_ok:
                print(f"         Expected: {expected!r}")
                print(f"         Got:      {result!r}")
            failed += 1

    print(f"\n  Total: {passed} passed, {failed} failed")
    return failed == 0


def test_strip_ansi_preserves_content():
    """Verify strip_ansi preserves all non-escape content."""
    print("\n" + "=" * 60)
    print("Content Preservation Tests")
    print("=" * 60)

    cases = [
        ("plain text", "hello world", "hello world"),
        ("with newlines", "line1\nline2\nline3", "line1\nline2\nline3"),
        ("with unicode", "café résumé", "café résumé"),
        ("with numbers", "port 3306", "port 3306"),
        ("empty string", "", ""),
        ("only escapes", "\x1b[31m\x1b[0m", ""),
    ]

    passed = 0
    for name, input_text, expected in cases:
        result = strip_ansi(input_text)
        if result == expected:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}: expected {expected!r}, got {result!r}")

    print(f"\n  Total: {passed}/{len(cases)} passed")
    return passed == len(cases)


def test_bytes_input():
    """Test with bytes input (raw PTY output)."""
    print("\n" + "=" * 60)
    print("Bytes Input Tests")
    print("=" * 60)

    payload = b"\x1b[31mERROR\x1b[0m: connection refused\n"
    result = sanitize_output(payload)

    if "\x1b" not in result and "ERROR" in result and "connection refused" in result:
        print("  [PASS] Bytes input sanitized correctly")
        return True
    else:
        print(f"  [FAIL] Bytes input: {result!r}")
        return False


def main():
    """Run all security tests."""
    print("\nSpyro Security Tests — ANSI Escape Sanitization")
    print("=" * 60)

    results = []
    results.append(("Attack payloads", test_all_attacks()))
    results.append(("Content preservation", test_strip_ansi_preserves_content()))
    results.append(("Bytes input", test_bytes_input()))

    print("\n" + "=" * 60)
    print("SUMMARY:")
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    if all_pass:
        print("\nAll security tests passed.")
    else:
        print("\nSome security tests FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    main()
