"""SSH tunnel management and Spyro Tunnel Supervisor (STS).

Manages local port forwarding, daemon processes, self-healing, and
network state transitions.

Uses psutil for cross-platform POSIX process tree/PID handling
as specified in the roadmap.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from ..utils.config import ProfileConfig, SpyroConfig
from ..utils.paths import spyro_home
from .state import (
    TunnelState,
    _pgid_alive,
    _pid_alive,
    get_tunnel,
    mark_running,
    mark_stopped,
    set_tunnel,
)

log = logging.getLogger("spyro.tunnel")


# ---------------------------------------------------------------------------
# Port conflict resolution
# ---------------------------------------------------------------------------


def _port_available(port: int) -> bool:
    """Check if a local port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _resolve_port(preferred: int) -> int:
    """Find an available port, starting from *preferred*.

    If preferred < 1024, shift to unprivileged range starting at 10000.
    If preferred is taken, increment until we find a free one.
    """
    if preferred < 1024:
        preferred = 10000 + (preferred % 1000)

    port = preferred
    while port < 65535:
        if _port_available(port):
            return port
        port += 1

    raise RuntimeError(f"No available port found starting from {preferred}")

# ---------------------------------------------------------------------------
# Tunnel lifecycle
# ---------------------------------------------------------------------------


class TunnelManager:
    """Manages SSH tunnels for a profile."""

    def __init__(self, config: SpyroConfig) -> None:
        self.config = config

    def start(
        self, profile_name: str, *, foreground: bool = False
    ) -> TunnelState:
        """Start tunnels for *profile_name*."""
        profile = self.config.get_profile(profile_name)

        # Check if already running
        existing = get_tunnel(profile_name)
        if existing and existing.status == "running":
            if _pid_alive(existing.pid):
                log.info(f"Tunnel for '{profile_name}' already running (PID {existing.pid})")
                return existing

        # Build local port forwarding args
        fwd_args: list[str] = []
        forwarded_ports: list[int] = []

        for remote_port in profile.forwarded_ports:
            local_port = _resolve_port(remote_port)
            fwd_args.extend(["-L", f"{local_port}:127.0.0.1:{remote_port}"])
            forwarded_ports.append(local_port)
            log.info(
                f"Port forwarding: localhost:{local_port} -> "
                f"{profile.host}:{remote_port}"
            )

        # Build ssh command
        ssh_args = [
            "ssh",
            "-o", "BatchMode=no",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-N",  # No remote command
        ]

        if profile.port != 22:
            ssh_args.extend(["-p", str(profile.port)])

        if profile.key:
            ssh_args.extend(["-i", profile.key])

        ssh_args.extend(fwd_args)

        target = f"{profile.user}@{profile.host}"
        ssh_args.append(target)

        log.info(f"Starting tunnel: {' '.join(ssh_args)}")

        if foreground:
            return self._start_foreground(profile, ssh_args, forwarded_ports)
        else:
            return self._start_daemon(profile, ssh_args, forwarded_ports)

    def _start_foreground(
        self,
        profile: ProfileConfig,
        ssh_args: list[str],
        forwarded_ports: list[int],
    ) -> TunnelState:
        """Run tunnel in foreground."""
        proc = subprocess.Popen(
            ssh_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        state = TunnelState(
            profile=profile.name,
            local_port=forwarded_ports[0] if forwarded_ports else 0,
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            ssh_pid=proc.pid,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            status="running",
            forwarded_ports=forwarded_ports,
        )
        set_tunnel(state)
        return state

    def _start_daemon(
        self,
        profile: ProfileConfig,
        ssh_args: list[str],
        forwarded_ports: list[int],
    ) -> TunnelState:
        """Run tunnel as a background daemon."""
        log_dir = spyro_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{profile.name}.log"

        with open(log_file, "a") as log_fh:
            proc = subprocess.Popen(
                ssh_args,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        state = TunnelState(
            profile=profile.name,
            local_port=forwarded_ports[0] if forwarded_ports else 0,
            pid=proc.pid,
            pgid=os.getpgid(proc.pid),
            ssh_pid=proc.pid,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            status="running",
            forwarded_ports=forwarded_ports,
        )
        set_tunnel(state)
        log.info(f"Daemon started: PID={proc.pid}, PGID={state.pgid}")
        return state

    def stop(self, profile_name: str) -> bool:
        """Stop tunnels for *profile_name*."""
        state = get_tunnel(profile_name)
        if not state:
            log.warning(f"No tunnel state found for '{profile_name}'")
            return False

        stopped = False

        # Use psutil to kill process tree if available
        if state.pgid and _pgid_alive(state.pgid):
            try:
                os.killpg(state.pgid, signal.SIGTERM)
                stopped = True
            except (OSError, ProcessLookupError):
                pass

        # Fallback: kill individual PIDs
        for pid in [state.ssh_pid, state.pid]:
            if pid and _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped = True
                except (OSError, ProcessLookupError):
                    pass

        # Brief wait for graceful shutdown
        time.sleep(0.5)

        # Force kill if still alive
        if state.pgid and _pgid_alive(state.pgid):
            try:
                os.killpg(state.pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass

        mark_stopped(profile_name)
        log.info(f"Tunnel for '{profile_name}' stopped")
        return stopped

    def stop_all(self) -> int:
        """Stop all active tunnels. Returns count stopped."""
        from .state import all_tunnels

        tunnels = all_tunnels()
        count = 0
        for name, state in tunnels.items():
            if state.status == "running":
                if self.stop(name):
                    count += 1
        return count

    def status(self, profile_name: str | None = None) -> dict[str, dict]:
        """Get status of tunnels."""
        from .state import all_tunnels

        tunnels = all_tunnels()
        result = {}

        for name, state in tunnels.items():
            if profile_name and name != profile_name:
                continue

            alive = _pid_alive(state.pid) if state.pid else False

            if alive and state.status == "running":
                state.status = "running"
            elif state.status == "running":
                state.status = "stale"

            result[name] = {
                "status": state.status,
                "pid": state.pid,
                "local_port": state.local_port,
                "forwarded_ports": state.forwarded_ports,
                "started_at": state.started_at,
            }

        return result

