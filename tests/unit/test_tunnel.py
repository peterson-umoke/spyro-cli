"""Tests for spyro.tunnel — port resolution, tunnel lifecycle."""

from __future__ import annotations

import socket

import pytest

from src.supervisor.tunnel import _port_available, _resolve_port


class TestPortAvailable:
    def test_available_port(self):
        # High port should be available
        assert _port_available(49152) is True

    def test_occupied_port(self):
        # Bind a port and check it's occupied
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert _port_available(port) is False


class TestResolvePort:
    def test_privileged_port_shifted(self):
        """Ports below 1024 should be shifted to unprivileged range."""
        port = _resolve_port(80)
        assert port >= 10000

    def test_available_port_returned(self):
        """Available port should be returned as-is."""
        # 49152 is almost certainly free
        port = _resolve_port(49152)
        assert port == 49152

    def test_fallback_on_conflict(self):
        """Should find an alternative if preferred port is taken."""
        # Bind a port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            taken = s.getsockname()[1]
            # Request the taken port — should get a different one
            port = _resolve_port(taken)
            assert port != taken
            assert port > 0
