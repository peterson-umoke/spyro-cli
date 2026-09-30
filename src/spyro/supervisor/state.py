"""State management for tunnel tracking in ~/.spyro/tunnels.json."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

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
    status: str = "unknown"  # running | stopped | stale
    forwarded_ports: list[int] = field(default_factory=list)  # local ports, same order as the profile's
    control_path: str = ""  # ssh ControlMaster socket of a daemon tunnel

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
    except json.JSONDecodeError:
        # Keep the evidence (PIDs of live tunnels) instead of overwriting it.
        try:
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return {}
    except OSError:
        return {}


def _save_raw(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


@contextmanager
def _locked() -> Iterator[None]:
    """Serialise read-modify-write cycles between concurrent spyro processes."""
    lock = _state_path().with_name(_STATE_FILE + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


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
    with _locked():
        raw = _load_raw()
        raw[state.profile] = state.to_dict()
        _save_raw(raw)


def remove_tunnel(profile: str) -> None:
    """Remove a tunnel state entry."""
    with _locked():
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
        state.last_keepalive = datetime.now(timezone.utc).isoformat()
        set_tunnel(state)


def mark_stopped(profile: str) -> None:
    """Mark a tunnel as stopped."""
    state = get_tunnel(profile)
    if state:
        state.status = "stopped"
        set_tunnel(state)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: exists, but belongs to someone else
        return True


def tunnel_alive(state: TunnelState) -> bool:
    """True if the tunnel's process exists *and is still ours*.

    PIDs are recycled (after a reboot ``tunnels.json`` still holds old ones),
    so existence alone is not enough: the process must still be the ssh master
    we started, recognised by its ``.../tun-<hash>`` ControlPath argument.
    """
    if not state.pid or not _pid_alive(state.pid):
        return False
    try:
        cmd = subprocess.run(
            ["ps", "-ww", "-p", str(state.pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell; do not declare a working tunnel dead
    return "ssh" in cmd and "/tun-" in cmd


def cleanup_stale() -> list[str]:
    """Mark tunnels whose processes are gone as stale.

    Returns list of cleaned profile names.
    """
    cleaned = []
    for name, state in all_tunnels().items():
        if state.status == "running" and not tunnel_alive(state):
            state.status = "stale"
            set_tunnel(state)
            cleaned.append(name)
    return cleaned
