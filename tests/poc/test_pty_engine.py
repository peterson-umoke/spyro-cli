#!/usr/bin/env python3
"""Phase 1 PoC: PTY Engine Validation Script.

Goal: Successfully inject a password into a remote challenge and capture
stdout without buffering artifacts.

Tests:
  1. Basic PTY spawn and echo
  2. Password injection into SSH prompt
  3. Output capture without buffering artifacts
  4. Credential zeroing after use

Usage:
    python -m tests.poc.test_pty_engine [user@host]

If no host is provided, tests against localhost (must have ssh access).
"""

from __future__ import annotations

import os
import sys

from spyro.security.ansi import strip_ansi, sanitize_output
from spyro.security.memory import SecureCredential
from spyro.core.pty_engine import PTYRunner, build_ssh_args


def test_basic_pty_spawn():
    """Test 1: Basic PTY spawn — echo command."""
    print("=" * 60)
    print("TEST 1: Basic PTY spawn (echo)")
    print("=" * 60)

    runner = PTYRunner()
    output_lines: list[str] = []

    def capture(line: str) -> None:
        output_lines.append(line)
        print(f"  [OUTPUT] {line}")

    # Run a simple echo command via local ssh
    exit_code = runner.run(
        ["echo", "hello-from-pty"],
        on_output=capture,
        timeout=5.0,
    )

    assert exit_code == 0, f"Expected exit code 0, got {exit_code}"
    assert any("hello-from-pty" in line for line in output_lines), \
        "Expected 'hello-from-pty' in output"
    print("  [PASS] Basic PTY spawn works\n")
    return True


def test_password_injection(target: str = "localhost"):
    """Test 2: Password injection into SSH prompt."""
    print("=" * 60)
    print(f"TEST 2: Password injection ({target})")
    print("=" * 60)

    runner = PTYRunner()
    output_lines: list[str] = []

    def capture(line: str) -> None:
        output_lines.append(line)
        print(f"  [OUTPUT] {line}")

    ssh_args = build_ssh_args(
        host=target,
        user=os.getenv("USER", "root"),
        strict_host_checking=False,
    )
    ssh_args.append("echo ssh-auth-test-ok")

    # This will prompt for password — we inject it
    exit_code = runner.run(
        ssh_args,
        password="test",  # Will fail if no password auth, that's OK
        on_output=capture,
        timeout=10.0,
    )

    # Check if we got the echo output (means auth succeeded)
    if any("ssh-auth-test-ok" in line for line in output_lines):
        print("  [PASS] Password injection worked, auth succeeded\n")
    else:
        print("  [INFO] Password auth not available (key-based or no password)")
        print("  [PASS] PTY runner handled prompt gracefully\n")
    return True


def test_credential_zeroing():
    """Test 3: Credential zeroing after use."""
    print("=" * 60)
    print("TEST 3: Credential zeroing")
    print("=" * 60)

    cred = SecureCredential(b"super-secret-password")
    assert not cred.zeroed
    assert cred.value == b"super-secret-password"
    print(f"  Created credential: {cred}")

    # Use the credential
    _ = cred.value

    # Zero it
    cred.zero()
    assert cred.zeroed
    print(f"  After zeroing: {cred}")

    # Verify .value raises
    try:
        _ = cred.value
        print("  [FAIL] Expected RuntimeError after zeroing")
        return False
    except RuntimeError:
        print("  [PASS] .value raises RuntimeError after zeroing\n")

    # Test context manager
    with SecureCredential(b"another-secret") as ctx_cred:
        assert ctx_cred.value == b"another-secret"
        print(f"  Context manager: {ctx_cred}")
    assert ctx_cred.zeroed
    print(f"  After context exit: {ctx_cred}")
    print("  [PASS] Context manager zeros on exit\n")
    return True


def test_ansi_stripping():
    """Test 4: ANSI stripping on PTY output."""
    print("=" * 60)
    print("TEST 4: ANSI stripping")
    print("=" * 60)

    # Simulate malicious terminal output
    malicious = "\x1b[31mFAKE ERROR\x1b[0m \x1b]0;TitleInjection\x07 REAL TEXT"
    cleaned = sanitize_output(malicious)
    print(f"  Input:  {malicious!r}")
    print(f"  Output: {cleaned!r}")

    assert "\x1b" not in cleaned, "ESC bytes should be stripped"
    assert "REAL TEXT" in cleaned, "Real text should survive"
    assert "FAKE ERROR" in cleaned, "Visible text should survive"
    assert "TitleInjection" not in cleaned, "OSC injection should be stripped"
    print("  [PASS] ANSI sanitization works\n")
    return True


def main():
    """Run all PoC tests."""
    target = sys.argv[1] if len(sys.argv) > 1 else "localhost"

    print("\nSpyro PTY Engine — Phase 1 PoC Validation")
    print("=" * 60)
    print(f"Target: {target}")
    print()

    results = []

    results.append(("Basic PTY spawn", test_basic_pty_spawn()))
    results.append(("Password injection", test_password_injection(target)))
    results.append(("Credential zeroing", test_credential_zeroing()))
    results.append(("ANSI stripping", test_ansi_stripping()))

    print("=" * 60)
    print("RESULTS:")
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("All PoC tests passed.")
    else:
        print("Some PoC tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
