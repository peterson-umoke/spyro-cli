"""SSH tunnel management.

A daemon tunnel is ``ssh -f -N -L ...`` run through the PTY engine (so keychain
passwords are injected exactly like every other spyro command) that becomes its
own ControlMaster. ``ssh -f`` only backgrounds itself once authentication and
the port forwards have succeeded, so a failed tunnel surfaces as an error
instead of a recorded PID that is already dead. Stopping goes through the
control socket; signals are a fallback and only ever sent to a PID that is
verifiably still an ssh/spyro process.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import signal
import socket
import subprocess
import time
from typing import Callable

from ..core.pty_engine import PTYRunner
from ..utils.config import ProfileConfig, SpyroConfig
from ..utils.keychain import get_credential
from ..utils.paths import sockets_dir, spyro_home
from .state import (
    TunnelState,
    all_tunnels,
    get_tunnel,
    mark_stopped,
    set_tunnel,
    tunnel_alive,
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


def _wait_for_ports(ports: list[int], timeout: float = 5.0) -> bool:
    """Wait until every local forward accepts connections."""
    deadline = time.monotonic() + timeout
    pending = list(ports)
    while pending and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", pending[0]), timeout=0.3):
                pending.pop(0)
        except OSError:
            time.sleep(0.1)
    return not pending


def db_local_port(profile: ProfileConfig, state: TunnelState) -> int:
    """Local port that forwards the profile's database port.

    ``state.forwarded_ports`` holds local ports in the same order as
    ``profile.forwarded_ports`` (remote ports), so the DB's local port is found
    by position rather than assuming the DB is the first forward.
    """
    try:
        return state.forwarded_ports[profile.forwarded_ports.index(profile.db.port)]
    except (ValueError, IndexError):
        return state.local_port


# ---------------------------------------------------------------------------
# Tunnel lifecycle
# ---------------------------------------------------------------------------


def _control_path(profile_name: str) -> str:
    digest = hashlib.sha1(profile_name.encode()).hexdigest()[:16]
    return str(sockets_dir() / f"tun-{digest}")


class TunnelManager:
    """Manages SSH tunnels for a profile."""

    def __init__(self, config: SpyroConfig) -> None:
        self.config = config

    def start(
        self,
        profile_name: str,
        *,
        foreground: bool = False,
        on_ready: Callable[[TunnelState], None] | None = None,
    ) -> TunnelState:
        """Start tunnels for *profile_name*.

        Raises RuntimeError (with ssh's own output) if the tunnel cannot be
        established. In foreground mode this blocks until ssh exits; *on_ready*
        is called first so the caller can tell the user.
        """
        profile = self.config.get_profile(profile_name)

        existing = get_tunnel(profile_name)
        if existing and existing.status == "running" and tunnel_alive(existing):
            log.info(f"Tunnel for '{profile_name}' already running (PID {existing.pid})")
            return existing

        if not profile.forwarded_ports:
            raise RuntimeError(f"'{profile_name}' has no forwarded_ports configured")

        fwd_args: list[str] = []
        local_ports: list[int] = []
        for remote_port in profile.forwarded_ports:
            local_port = _resolve_port(remote_port)
            while local_port in local_ports:  # two profiles' ports must not collide
                local_port = _resolve_port(local_port + 1)
            fwd_args.extend(["-L", f"{local_port}:127.0.0.1:{remote_port}"])
            local_ports.append(local_port)
            log.info(
                f"Port forwarding: localhost:{local_port} -> "
                f"{profile.host}:{remote_port}"
            )

        ssh_args = [
            "ssh",
            "-o", "BatchMode=no",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
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
        password = get_credential(profile_name, profile.user) or ""

        if foreground:
            return self._run_foreground(profile, ssh_args, target, local_ports, password, on_ready)
        return self._start_daemon(profile, ssh_args, target, local_ports, password)

    def ensure(self, profile_name: str) -> TunnelState:
        """A live tunnel for *profile_name*, starting one if needed."""
        state = get_tunnel(profile_name)
        if state and state.status == "running" and tunnel_alive(state):
            return state
        return self.start(profile_name)

    def _new_state(self, profile: ProfileConfig, local_ports: list[int], pid: int, ctl: str = "") -> TunnelState:
        return TunnelState(
            profile=profile.name,
            local_port=local_ports[0],
            pid=pid,
            ssh_pid=pid,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            status="running",
            forwarded_ports=local_ports,
            control_path=ctl,
        )

    def _run_foreground(
        self,
        profile: ProfileConfig,
        ssh_args: list[str],
        target: str,
        local_ports: list[int],
        password: str,
        on_ready: Callable[[TunnelState], None] | None,
    ) -> TunnelState:
        """Start the tunnel, then hold this terminal until Ctrl+C (or `spyro down`).

        Authentication goes through the same PTY path as a daemon tunnel; an
        interactive PTY session would treat a silent `ssh -N` as "still logging
        in" and kill it at the auth timeout.
        """
        state = self._start_daemon(profile, ssh_args, target, local_ports, password)
        if on_ready:
            on_ready(state)

        # Ctrl+C, `kill` and a closed terminal (SIGHUP) all end the tunnel:
        # a foreground tunnel must not outlive its terminal as an orphaned ssh.
        def _interrupt(*_: object) -> None:
            raise KeyboardInterrupt

        previous = {}
        try:
            for sig in (signal.SIGTERM, signal.SIGHUP):
                previous[sig] = signal.signal(sig, _interrupt)
        except ValueError:  # not the main thread (tests); default handling applies
            pass
        try:
            while tunnel_alive(state):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            self.stop(profile.name)
        return state

    def _start_daemon(
        self,
        profile: ProfileConfig,
        ssh_args: list[str],
        target: str,
        local_ports: list[int],
        password: str,
    ) -> TunnelState:
        """Authenticate, let ssh background itself, and verify the forwards work."""
        log_dir = spyro_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ctl = _control_path(profile.name)
        self._clear_stale_master(ctl, target)

        args = ssh_args + ["-f", "-o", "ControlMaster=yes", "-o", f"ControlPath={ctl}", target]
        output: list[str] = []
        exit_code = PTYRunner().run(args, password=password, on_output=output.append, timeout=30.0)

        text = "\n".join(line for line in output if line.strip())
        if text:
            with open(log_dir / f"{profile.name}.log", "a") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {text}\n")
        if exit_code != 0:
            detail = text.splitlines()[-1] if text else f"ssh exited with code {exit_code}"
            raise RuntimeError(detail)

        pid = self._master_pid(ctl, target)
        if not pid:
            raise RuntimeError("ssh started but its process could not be found")
        if not _wait_for_ports(local_ports):
            self._exit_master(ctl, target)
            raise RuntimeError("tunnel started but the forwarded ports are not accepting connections")

        state = self._new_state(profile, local_ports, pid, ctl)
        set_tunnel(state)
        log.info(f"Daemon started: PID={pid}")
        return state

    @classmethod
    def _clear_stale_master(cls, ctl: str, target: str) -> None:
        """Remove what a previous tunnel left at *ctl*.

        A leftover socket (kill -9, reboot) makes ssh say "ControlSocket already
        exists, disabling multiplexing": it then runs a master nobody can find
        or stop. A *live* master that state does not track (an interrupted
        `up`) would be mistaken for the new one, so it is shut down first.
        """
        if not os.path.exists(ctl):
            return
        if cls._master_pid(ctl, target):
            cls._exit_master(ctl, target)
        try:
            os.unlink(ctl)
        except FileNotFoundError:
            pass

    @staticmethod
    def _master_pid(ctl: str, target: str) -> int:
        try:
            res = subprocess.run(
                ["ssh", "-S", ctl, "-O", "check", target],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        m = re.search(r"pid=(\d+)", res.stdout + res.stderr)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _exit_master(ctl: str, target: str) -> bool:
        try:
            res = subprocess.run(
                ["ssh", "-S", ctl, "-O", "exit", target],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0

    def stop(self, profile_name: str) -> bool:
        """Stop tunnels for *profile_name*."""
        state = get_tunnel(profile_name)
        if not state:
            log.warning(f"No tunnel state found for '{profile_name}'")
            return False

        try:
            p = self.config.get_profile(profile_name)
            target = f"{p.user}@{p.host}"
        except SystemExit:  # profile was removed from spyro.toml since
            target = "spyro-tunnel"

        stopped = False
        if state.control_path and os.path.exists(state.control_path):
            stopped = self._exit_master(state.control_path, target)

        # Fallback: signal the PID, but only if it is still our ssh/spyro.
        if not stopped and tunnel_alive(state):
            try:
                os.kill(state.pid, signal.SIGTERM)
                stopped = True
            except OSError:
                pass
            for _ in range(20):  # up to 2s for a graceful exit
                if not tunnel_alive(state):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.kill(state.pid, signal.SIGKILL)
                except OSError:
                    pass

        mark_stopped(profile_name)
        if stopped:
            log.info(f"Tunnel for '{profile_name}' stopped")
        else:
            log.info(f"Tunnel for '{profile_name}' was not running; state cleared")
        return stopped

    def stop_all(self) -> int:
        """Stop all active tunnels. Returns count stopped."""
        count = 0
        for name, state in all_tunnels().items():
            if state.status == "running" and self.stop(name):
                count += 1
        return count

    def status(self, profile_name: str | None = None) -> dict[str, dict]:
        """Get status of tunnels (a recorded tunnel whose process died is ``stale``)."""
        result = {}
        for name, state in all_tunnels().items():
            if profile_name and name != profile_name:
                continue

            if state.status == "running" and not tunnel_alive(state):
                state.status = "stale"
                set_tunnel(state)

            result[name] = {
                "status": state.status,
                "pid": state.pid,
                "local_port": state.local_port,
                "forwarded_ports": state.forwarded_ports,
                "started_at": state.started_at,
            }

        return result
