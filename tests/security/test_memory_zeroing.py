"""Security tests: SecureCredential zeroes its buffer and refuses reads afterwards.

Note: this only covers the bytearray that SecureCredential owns. Copies made by
callers (``.value`` returns an immutable ``bytes``; ``str`` passwords) are not
zeroable in CPython, so zeroing is best-effort hygiene, not a guarantee.
"""

from __future__ import annotations

import sys

import pytest

from spyro.security.memory import SecureCredential


def test_basic_zeroing():
    cred = SecureCredential(b"secret-password-123")
    assert not cred.zeroed
    assert cred.value == b"secret-password-123"

    buf = cred._data  # keep a reference to the original buffer
    cred.zero()

    assert cred.zeroed
    assert bytes(buf) == b"\x00" * len(buf)
    assert bytes(cred._data) == b"\x00" * len(cred._data)


def test_value_raises_after_zero():
    cred = SecureCredential(b"will-be-zeroed")
    cred.zero()
    with pytest.raises(RuntimeError):
        _ = cred.value


def test_context_manager():
    with SecureCredential(b"context-secret") as cred:
        assert not cred.zeroed
        assert cred.value == b"context-secret"
    assert cred.zeroed


def test_multiple_zero_calls():
    cred = SecureCredential(b"double-zero")
    cred.zero()
    cred.zero()
    cred.zero()
    assert cred.zeroed


def test_empty_credential():
    cred = SecureCredential(b"")
    assert not cred.zeroed
    cred.zero()
    assert cred.zeroed


def test_del_zeros():
    cred = SecureCredential(b"will-be-deleted")
    assert not cred.zeroed
    cred.__del__()
    assert cred.zeroed


def test_str_input_is_encoded_and_zeroed():
    cred = SecureCredential("very-secret-password")
    assert cred.value == b"very-secret-password"
    cred.zero()
    assert bytes(cred._data) == b"\x00" * len("very-secret-password")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
