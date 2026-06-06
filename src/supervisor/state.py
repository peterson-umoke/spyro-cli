"""State management for tunnel tracking in ~/.spyro/tunnels.json."""

from __future__ import annotations

import json
import os
import signal
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..utils.paths import spyro_home


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class TunnelState:
    """State for a single active tunnel."""

    profile: str
    local_port: int
    remote_host: str = "127.0.0.1"
    remote_port: int = 3306
    pid: int = 0
    pgid: int = 0
    ssh_pid: int = 0
    started_at: str = ""
    last_keepalive: str = ""
    status: str = "unknown"  # running | stopped | error
    forwarded_ports: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TunnelState:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------

_STATE_FILE = "tunnels.json"


def _state_path() -> Path:
    return spyro_home() / _STATE_FILE


def _load_raw() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tunnel(profile: str) -> TunnelState | None:
    """Get the tunnel state for a profile, or None."""
    raw = _load_raw()
    entry = raw.get(profile)
    if not entry:
        return None
    return TunnelState.from_dict(entry)


def set_tunnel(state: TunnelState) -> None:
    """Upsert a tunnel state entry."""
    raw = _load_raw()
    raw[state.profile] = state.to_dict()
    _save_raw(raw)


def remove_tunnel(profile: str) -> None:
    """Remove a tunnel state entry."""
    raw = _load_raw()
    raw.pop(profile, None)
    _save_raw(raw)


def all_tunnels() -> dict[str, TunnelState]:
    """Return all tunnel states."""
    raw = _load_raw()
    return {k: TunnelState.from_dict(v) for k, v in raw.items()}


def mark_running(profile: str) -> None:
    """Mark a tunnel as running, update timestamp."""
    state = get_tunnel(profile)
    if state:
        state.status = "running"
        state.last_keepalive = _now()
        set_tunnel(state)


def mark_stopped(profile: str) -> None:
    """Mark a tunnel as stopped."""
    state = get_tunnel(profile)
    if state:
        state.status = "stopped"
        set_tunnel(state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Process cleanup
# ---------------------------------------------------------------------------


def _pgid_exists(pgid: int) -> bool:
    """Check if a process group still exists."""
    try:
        os.killpg(pgid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def kill_tunnels(profiles: list[str] | None = None) -> int:
    """Kill tunnel processes for given profiles (or all).

    Returns the number of processes killed.
    """
    tunnels = all_tunnels()
    killed = 0

    for name, state in tunnels.items():
        if profiles and name not in profiles:
            continue
        if state.pgid and _pgid_exists(state.pgid):
            try:
                os.killpg(state.pgid, signal.SIGTERM)
                killed += 1
            except (OSError, ProcessLookupError):
                pass
        elif state.pid and _pid_alive(state.pid):
            try:
                os.kill(state.pid, signal.SIGTERM)
                killed += 1
            except (OSError, ProcessLookupError):
                pass

    return killed


def cleanup_stale() -> list[str]:
    """Remove state entries for tunnels whose processes are dead.

    Returns list of cleaned profile names.
    """
    tunnels = all_tunnels()
    cleaned = []

    for name, state in tunnels.items():
        alive = False
        if state.pgid:
            alive = _pgid_exists(state.pgid)
        if not alive and state.pid:
            alive = _pid_alive(state.pid)

        if not alive and state.status == "running":
            state.status = "stale"
            set_tunnel(state)
            cleaned.append(name)

    return cleaned


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
