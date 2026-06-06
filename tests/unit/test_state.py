"""Tests for spyro.state — tunnel state management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.supervisor.state import (
    TunnelState,
    all_tunnels,
    cleanup_stale,
    get_tunnel,
    mark_running,
    mark_stopped,
    remove_tunnel,
    set_tunnel,
)


class TestTunnelState:
    def test_to_dict(self):
        state = TunnelState(
            profile="staging",
            local_port=33060,
            pid=1234,
            status="running",
        )
        d = state.to_dict()
        assert d["profile"] == "staging"
        assert d["local_port"] == 33060
        assert d["pid"] == 1234
        assert d["status"] == "running"

    def test_from_dict(self):
        d = {
            "profile": "production",
            "local_port": 5432,
            "pid": 5678,
            "status": "stopped",
            "pgid": 5600,
            "ssh_pid": 5679,
            "started_at": "2026-01-01T00:00:00+00:00",
            "forwarded_ports": [5432],
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
            "last_keepalive": "",
        }
        state = TunnelState.from_dict(d)
        assert state.profile == "production"
        assert state.local_port == 5432
        assert state.pid == 5678
        assert state.status == "stopped"

    def test_from_dict_extra_keys_ignored(self):
        d = {
            "profile": "test",
            "local_port": 1234,
            "unknown_field": "ignored",
        }
        state = TunnelState.from_dict(d)
        assert state.profile == "test"


@pytest.fixture(autouse=True)
def _patch_state_file(tmp_path, monkeypatch):
    """Redirect state file to a temp directory for each test."""
    state_file = tmp_path / "tunnels.json"
    monkeypatch.setattr("src.supervisor.state._state_path", lambda: state_file)
    return state_file


class TestStateOperations:
    def test_set_and_get(self):
        state = TunnelState(
            profile="staging",
            local_port=33060,
            pid=1234,
            status="running",
        )
        set_tunnel(state)
        retrieved = get_tunnel("staging")
        assert retrieved is not None
        assert retrieved.profile == "staging"
        assert retrieved.local_port == 33060

    def test_get_missing(self):
        assert get_tunnel("nonexistent") is None

    def test_remove(self):
        state = TunnelState(profile="staging", local_port=33060, pid=1234)
        set_tunnel(state)
        remove_tunnel("staging")
        assert get_tunnel("staging") is None

    def test_remove_nonexistent(self):
        # Should not raise
        remove_tunnel("nonexistent")

    def test_all_tunnels(self):
        set_tunnel(TunnelState(profile="a", local_port=1111, pid=1))
        set_tunnel(TunnelState(profile="b", local_port=2222, pid=2))
        tunnels = all_tunnels()
        assert len(tunnels) == 2
        assert "a" in tunnels
        assert "b" in tunnels

    def test_upsert(self):
        set_tunnel(TunnelState(profile="a", local_port=1111, pid=1))
        set_tunnel(TunnelState(profile="a", local_port=2222, pid=2))
        tunnels = all_tunnels()
        assert len(tunnels) == 1
        assert tunnels["a"].local_port == 2222

    def test_mark_running(self):
        set_tunnel(TunnelState(profile="a", local_port=1111, pid=1, status="stopped"))
        mark_running("a")
        state = get_tunnel("a")
        assert state.status == "running"
        assert state.last_keepalive != ""

    def test_mark_stopped(self):
        set_tunnel(TunnelState(profile="a", local_port=1111, pid=1, status="running"))
        mark_stopped("a")
        state = get_tunnel("a")
        assert state.status == "stopped"

    def test_persistence(self):
        """State should survive across load/save cycles."""
        state = TunnelState(profile="persist", local_port=9999, pid=42, status="running")
        set_tunnel(state)
        # Simulate reload by reading raw file
        raw = json.loads(Path(_state_file_path()).read_text())
        assert "persist" in raw
        assert raw["persist"]["local_port"] == 9999


def _state_file_path() -> str:
    """Helper to get the current state file path."""
    from src.supervisor.state import _state_path
    return str(_state_path())
