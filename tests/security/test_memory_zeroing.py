"""Phase 4 Security Test: Memory zeroing verification.

Tests that SecureCredential properly zeros memory after use and
that zeroed credentials cannot be accessed.
"""

from __future__ import annotations

import os
import sys

from spyro.security.memory import SecureCredential, SecureString


def test_basic_zeroing():
    """Test that zero() overwrites the internal buffer."""
    print("=" * 60)
    print("TEST: Basic zeroing")
    print("=" * 60)

    cred = SecureCredential(b"secret-password-123")
    assert not cred.zeroed
    assert cred.value == b"secret-password-123"
    print(f"  Created: {cred}")

    # Get reference to internal buffer before zeroing
    buf = cred._data
    original_bytes = bytes(buf)

    cred.zero()

    assert cred.zeroed
    print(f"  After zero: {cred}")

    # The internal buffer should now be all zeros
    after_bytes = bytes(buf)
    assert after_bytes == b"\x00" * len(after_bytes), \
        f"Buffer not zeroed: {after_bytes!r}"

    print("  [PASS] Internal buffer is all zeros")
    return True


def test_value_raises_after_zero():
    """Test that accessing .value after zero raises RuntimeError."""
    print("\n" + "=" * 60)
    print("TEST: .value raises after zero")
    print("=" * 60)

    cred = SecureCredential(b"will-be-zeroed")
    cred.zero()

    try:
        _ = cred.value
        print("  [FAIL] Expected RuntimeError")
        return False
    except RuntimeError as e:
        print(f"  [PASS] RuntimeError raised: {e}")
        return True


def test_context_manager():
    """Test that context manager zeros on exit."""
    print("\n" + "=" * 60)
    print("TEST: Context manager zeroing")
    print("=" * 60)

    with SecureCredential(b"context-secret") as cred:
        assert not cred.zeroed
        assert cred.value == b"context-secret"
        print(f"  Inside context: {cred}")

    assert cred.zeroed
    print(f"  After context exit: {cred}")
    print("  [PASS] Context manager zeros on exit")
    return True


def test_multiple_zero_calls():
    """Test that calling zero() multiple times is safe."""
    print("\n" + "=" * 60)
    print("TEST: Multiple zero() calls")
    print("=" * 60)

    cred = SecureCredential(b"double-zero")
    cred.zero()
    cred.zero()  # Should not raise
    cred.zero()  # Should not raise

    assert cred.zeroed
    print("  [PASS] Multiple zero() calls are idempotent")
    return True


def test_empty_credential():
    """Test zeroing an empty credential."""
    print("\n" + "=" * 60)
    print("TEST: Empty credential zeroing")
    print("=" * 60)

    cred = SecureCredential(b"")
    assert not cred.zeroed
    cred.zero()
    assert cred.zeroed
    print("  [PASS] Empty credential zeros cleanly")
    return True


def test_del_zeros():
    """Test that __del__ calls zero."""
    print("\n" + "=" * 60)
    print("TEST: __del__ triggers zero")
    print("=" * 60)

    cred = SecureCredential(b"will-be-deleted")
    assert not cred.zeroed

    # Manually trigger what __del__ would do
    cred.__del__()

    assert cred.zeroed
    print("  [PASS] __del__ triggers zero")
    return True


def test_secure_string():
    """Test SecureString variant."""
    print("\n" + "=" * 60)
    print("TEST: SecureString")
    print("=" * 60)

    ss = SecureString("string-secret")
    assert not ss.zeroed
    assert ss.value == "string-secret"
    assert ss.bytes_value == b"string-secret"
    print(f"  Created: {ss}")

    ss.zero()
    assert ss.zeroed
    print(f"  After zero: {ss}")

    try:
        _ = ss.value
        print("  [FAIL] Expected RuntimeError")
        return False
    except RuntimeError:
        print("  [PASS] SecureString works correctly")
        return True


def test_no_lingering_refs():
    """Test that zeroing doesn't leave string references."""
    print("\n" + "=" * 60)
    print("TEST: No lingering string references")
    print("=" * 60)

    secret = "very-secret-password"
    cred = SecureCredential(secret)

    # Use the credential
    _ = cred.value

    # Zero it
    cred.zero()

    # Verify the internal buffer is zeroed
    # (Can't directly check for string references in CPython, but we
    # can verify the bytearray is zeroed)
    buf_bytes = bytes(cred._data)
    assert buf_bytes == b"\x00" * len(buf_bytes)

    print("  [PASS] Internal buffer fully zeroed")
    return True


def main():
    """Run all memory security tests."""
    print("\nSpyro Security Tests — Memory Zeroing")
    print("=" * 60)

    results = []
    results.append(("Basic zeroing", test_basic_zeroing()))
    results.append((".value raises after zero", test_value_raises_after_zero()))
    results.append(("Context manager", test_context_manager()))
    results.append(("Multiple zero calls", test_multiple_zero_calls()))
    results.append(("Empty credential", test_empty_credential()))
    results.append(("__del__ trigger", test_del_zeros()))
    results.append(("SecureString", test_secure_string()))
    results.append(("No lingering refs", test_no_lingering_refs()))

    print("\n" + "=" * 60)
    print("SUMMARY:")
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    if all_pass:
        print("\nAll memory security tests passed.")
    else:
        print("\nSome memory security tests FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    main()
