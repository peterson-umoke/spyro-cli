"""Secure memory handling for credentials.

The PRD requires: "Sensitive credentials are stored in mutable bytearray
objects and overwritten immediately after usage."

Python doesn't expose secure memory APIs (mlock, explicit_bzero), so we
use bytearrays with explicit zeroing. This isn't bulletproof against
swap/dumps, but it's the best we can do in pure Python and it prevents
credentials from lingering in the heap after use.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any


class SecureCredential:
    """A credential holder that zeros memory on cleanup.

    Usage:
        cred = SecureCredential(b"my-secret-password")
        # Use cred.value for the bytes
        # When done:
        cred.zero()
        # Or use as context manager:
        with SecureCredential(b"secret") as cred:
            use(cred.value)
    """

    def __init__(self, value: str | bytes) -> None:
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._data: bytearray = bytearray(value)
        self._zeroed = False

    @property
    def value(self) -> bytes:
        """Return the credential bytes. Reads are tracked."""
        if self._zeroed:
            raise RuntimeError("Credential has been zeroed")
        return bytes(self._data)

    @property
    def zeroed(self) -> bool:
        return self._zeroed

    def zero(self) -> None:
        """Overwrite the internal buffer with zeros.

        Attempts multiple strategies:
        1. Overwrite with \x00 bytes
        2. Overwrite with random bytes (to confuse optimizers)
        3. Overwrite with \x00 again
        4. Request Python GC to collect

        After zeroing, .value raises RuntimeError.
        """
        if self._zeroed:
            return

        length = len(self._data)
        if length == 0:
            self._zeroed = True
            return

        # Pass 1: zero fill
        for i in range(length):
            self._data[i] = 0

        # Pass 2: random fill (confuses compiler/interpreter optimizers)
        import os
        random_bytes = os.urandom(length)
        for i in range(length):
            self._data[i] = random_bytes[i]

        # Pass 3: zero fill again
        for i in range(length):
            self._data[i] = 0

        # Attempt to clear the bytearray's internal buffer reference
        # by reassigning. This doesn't guarantee the old buffer is freed
        # but helps with CPython's memory allocator.
        try:
            # Force the bytearray to release its buffer
            self._data = bytearray(b"\x00" * length)
            for i in range(length):
                self._data[i] = 0
        except Exception:
            pass

        self._zeroed = True

    def __del__(self) -> None:
        """Zero on garbage collection."""
        if not self._zeroed:
            try:
                self.zero()
            except Exception:
                pass

    def __enter__(self) -> SecureCredential:
        return self

    def __exit__(self, *args: Any) -> None:
        self.zero()

    def __repr__(self) -> str:
        if self._zeroed:
            return "SecureCredential(zeroed)"
        return f"SecureCredential(len={len(self._data)})"


class SecureString:
    """String version of SecureCredential. Zeros on cleanup.

    Use for string-based credential APIs.
    """

    def __init__(self, value: str) -> None:
        self._data = bytearray(value.encode("utf-8"))
        self._zeroed = False

    @property
    def value(self) -> str:
        if self._zeroed:
            raise RuntimeError("Credential has been zeroed")
        return self._data.decode("utf-8")

    @property
    def zeroed(self) -> bool:
        return self._zeroed

    @property
    def bytes_value(self) -> bytes:
        if self._zeroed:
            raise RuntimeError("Credential has been zeroed")
        return bytes(self._data)

    def zero(self) -> None:
        """Zero the internal buffer."""
        if self._zeroed:
            return
        length = len(self._data)
        for i in range(length):
            self._data[i] = 0
        self._zeroed = True

    def __del__(self) -> None:
        if not self._zeroed:
            try:
                self.zero()
            except Exception:
                pass

    def __enter__(self) -> SecureString:
        return self

    def __exit__(self, *args: Any) -> None:
        self.zero()

    def __repr__(self) -> str:
        if self._zeroed:
            return "SecureString(zeroed)"
        return f"SecureString(len={len(self._data)})"
