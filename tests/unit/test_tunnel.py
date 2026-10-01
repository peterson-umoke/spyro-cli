"""Tests for spyro.tunnel — port resolution, tunnel lifecycle."""

from __future__ import annotations

import socket

import pytest

from spyro.supervisor.tunnel import _port_available, _resolve_port


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


class TestTunnelArchitecture:
    def test_supervisor_yagni_removed(self):
        import spyro.supervisor.tunnel as tun
        assert not hasattr(tun, "HAS_PSUTIL")
        assert not hasattr(tun, "_pid_alive_psutil")
        assert not hasattr(tun, "TunnelSupervisor")


# ---------------------------------------------------------------------------
# Lifecycle, against a fake `ssh` that prompts for a password and, with -f,
# forks a real listener on the forwarded port (like the real thing would).
# ---------------------------------------------------------------------------

FAKE_SSH = r'''
import os, select, socket, sys, time
args = sys.argv[1:]

def opt(name):
    for i, a in enumerate(args):
        if a == "-o" and args[i + 1].startswith(name + "="):
            return args[i + 1].split("=", 1)[1]

if "-O" in args:                      # control-socket operations
    op, sock = args[args.index("-O") + 1], args[args.index("-S") + 1]
    try:
        pid = int(open(sock).read())
    except OSError:
        print("Control socket connect(%s): No such file or directory" % sock, file=sys.stderr)
        sys.exit(255)
    if op == "check":
        print("Master running (pid=%d)" % pid, file=sys.stderr); sys.exit(0)
    os.kill(pid, 15); os.remove(sock); print("Exit request sent.", file=sys.stderr); sys.exit(0)

ports = [int(args[i + 1].split(":")[0]) for i, a in enumerate(args) if a == "-L"]
sys.stdout.write("user@host's password: "); sys.stdout.flush()
if sys.stdin.readline().strip() != "right":
    sys.stdout.write("\nPermission denied, please try again.\nuser@host's password: ")
    sys.stdout.flush(); sys.stdin.readline(); sys.exit(255)
if os.environ.get("FAKE_SSH_FAIL_FORWARD"):
    print("\nbind [127.0.0.1]:%d: Address already in use" % ports[0]); sys.exit(255)
print()
if "-f" not in args:                  # foreground: "connected" for a moment
    time.sleep(0.5); sys.exit(0)
r, w = os.pipe()
if os.fork() == 0:
    os.setsid()
    socks = []
    for p in ports:
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", p)); s.listen(5); socks.append(s)
    open(opt("ControlPath"), "w").write(str(os.getpid()))
    os.write(w, b"x")
    while True:
        for s in select.select(socks, [], [], 1)[0]:
            s.accept()[0].close()
os.read(r, 1); sys.exit(0)
'''


@pytest.fixture
def fake_ssh(monkeypatch):
    """Fake `ssh` on PATH and a *short* HOME (so sockets stay under ~/.spyro)."""
    import os
    import shutil
    import signal
    import sys
    import tempfile
    from pathlib import Path

    home = Path(tempfile.mkdtemp(prefix="sp", dir="/tmp"))
    bindir = home / "bin"
    bindir.mkdir()
    script = bindir / "ssh"
    script.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SPYRO_PASSWORD", "right")
    monkeypatch.delenv("FAKE_SSH_FAIL_FORWARD", raising=False)
    yield home
    for f in (home / ".spyro" / "sockets").glob("tun-*"):  # leave no daemons behind
        try:
            os.kill(int(f.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
    shutil.rmtree(home, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _manager(port: int):
    from spyro.supervisor.tunnel import TunnelManager
    from spyro.utils.config import DatabaseConfig, ProfileConfig, SpyroConfig

    profile = ProfileConfig(
        name="a", host="h.example.com", user="u",
        forwarded_ports=[port], db=DatabaseConfig(port=port),
    )
    return TunnelManager(SpyroConfig(profiles={"a": profile}))


def _listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


class TestTunnelLifecycle:
    def test_password_host_tunnel_starts_and_stops_via_control_socket(self, fake_ssh):
        from spyro.supervisor.state import get_tunnel, tunnel_alive

        port = _free_port()
        mgr = _manager(port)
        state = mgr.start("a")

        assert state.status == "running" and state.forwarded_ports == [port]
        assert _listening(port)
        assert tunnel_alive(get_tunnel("a"))
        assert mgr.status("a")["a"]["status"] == "running"

        assert mgr.ensure("a").pid == state.pid          # reused, not restarted
        assert mgr.stop("a") is True
        assert not _listening(port)
        assert mgr.status("a")["a"]["status"] == "stopped"

    def test_rejected_password_raises_with_a_reason(self, fake_ssh, monkeypatch):
        import pytest
        from spyro.supervisor.state import get_tunnel

        monkeypatch.setenv("SPYRO_PASSWORD", "wrong")
        with pytest.raises(RuntimeError, match="rejected"):
            _manager(_free_port()).start("a")
        assert get_tunnel("a") is None                    # nothing recorded as running

    def test_failed_forward_raises_ssh_error_and_records_nothing(self, fake_ssh, monkeypatch):
        import pytest
        from spyro.supervisor.state import get_tunnel

        monkeypatch.setenv("FAKE_SSH_FAIL_FORWARD", "1")
        with pytest.raises(RuntimeError, match="Address already in use"):
            _manager(_free_port()).start("a")
        assert get_tunnel("a") is None

    def test_dead_tunnel_is_stale_and_ensure_restarts_it(self, fake_ssh):
        import os
        import signal
        import time
        from spyro.supervisor.state import get_tunnel

        port = _free_port()
        mgr = _manager(port)
        first = mgr.start("a")
        os.kill(first.pid, signal.SIGKILL)
        time.sleep(0.3)

        assert mgr.status("a")["a"]["status"] == "stale"
        second = mgr.ensure("a")
        assert second.pid != first.pid and _listening(port)
        assert get_tunnel("a").status == "running"

    def test_stop_never_signals_a_recycled_pid(self, fake_ssh, monkeypatch):
        """tunnels.json can outlive a reboot; the recorded PID may now be anything."""
        import subprocess
        from spyro.supervisor.state import TunnelState, set_tunnel

        victim = subprocess.Popen(["sleep", "30"])
        try:
            set_tunnel(TunnelState(profile="a", local_port=1, pid=victim.pid, pgid=victim.pid, status="running"))
            _manager(_free_port()).stop("a")
            assert victim.poll() is None                  # still running
        finally:
            victim.kill()
            victim.wait()

    def test_stopping_a_stale_tunnel_reports_nothing_stopped_and_clears_the_state(self, fake_ssh, caplog):
        import logging
        import subprocess
        from spyro.supervisor.state import TunnelState, get_tunnel, set_tunnel

        dead = subprocess.Popen(["true"])
        dead.wait()
        set_tunnel(TunnelState(profile="a", local_port=1, pid=dead.pid, pgid=dead.pid, status="running"))
        with caplog.at_level(logging.INFO):
            assert _manager(_free_port()).stop("a") is False
        assert get_tunnel("a").status == "stopped"
        assert "was not running" in caplog.text and "Tunnel for 'a' stopped" not in caplog.text

    def test_no_forwarded_ports_is_an_error_not_a_useless_ssh(self, fake_ssh):
        import pytest
        from spyro.supervisor.tunnel import TunnelManager
        from spyro.utils.config import ProfileConfig, SpyroConfig

        mgr = TunnelManager(SpyroConfig(profiles={"a": ProfileConfig(name="a", host="h")}))
        with pytest.raises(RuntimeError, match="no forwarded_ports"):
            mgr.start("a")

    def test_foreground_holds_until_the_tunnel_is_stopped(self, fake_ssh):
        import threading
        import time
        from spyro.supervisor.state import get_tunnel

        port = _free_port()
        mgr = _manager(port)
        ready = threading.Event()
        done = threading.Event()

        def run():
            mgr.start("a", foreground=True, on_ready=lambda st: ready.set())
            done.set()

        t = threading.Thread(target=run)
        t.start()
        assert ready.wait(10) and _listening(port)
        time.sleep(1.5)
        assert not done.is_set()                      # still holding the terminal
        _manager(port).stop("a")                      # e.g. `spyro down` from another terminal
        assert done.wait(10)
        t.join(5)
        assert not _listening(port) and get_tunnel("a").status == "stopped"

    def test_stale_control_socket_does_not_leak_an_untracked_ssh(self, fake_ssh):
        """kill -9 / reboot leaves the socket behind; the next `up` must still work
        and leave exactly one recorded tunnel."""
        import os
        import signal
        import time

        port = _free_port()
        mgr = _manager(port)
        first = mgr.start("a")
        os.kill(first.pid, signal.SIGKILL)            # socket file stays on disk
        time.sleep(0.3)
        assert os.path.exists(first.control_path)

        second = mgr.start("a")
        assert second.pid != first.pid and _listening(port)
        assert mgr.stop("a") is True and not _listening(port)

    def test_untracked_live_master_is_replaced_not_adopted(self, fake_ssh):
        import os
        import time
        from spyro.supervisor.state import remove_tunnel

        port = _free_port()
        mgr = _manager(port)
        first = mgr.start("a")
        remove_tunnel("a")                            # e.g. Ctrl+C left state without an entry
        second = mgr.start("a")
        assert second.pid != first.pid
        time.sleep(0.3)
        with pytest.raises(OSError):
            os.kill(first.pid, 0)                     # the old master was shut down
        mgr.stop("a")


class TestDbLocalPort:
    def test_uses_the_forward_of_the_db_port_not_the_first(self):
        from spyro.supervisor.state import TunnelState
        from spyro.supervisor.tunnel import db_local_port
        from spyro.utils.config import DatabaseConfig, ProfileConfig

        profile = ProfileConfig(name="a", host="h", forwarded_ports=[6379, 3306], db=DatabaseConfig(port=3306))
        state = TunnelState(profile="a", local_port=6379, forwarded_ports=[6379, 3307])
        assert db_local_port(profile, state) == 3307

    def test_falls_back_to_first_forward(self):
        from spyro.supervisor.state import TunnelState
        from spyro.supervisor.tunnel import db_local_port
        from spyro.utils.config import DatabaseConfig, ProfileConfig

        profile = ProfileConfig(name="a", host="h", forwarded_ports=[3306], db=DatabaseConfig(port=9999))
        assert db_local_port(profile, TunnelState(profile="a", local_port=3310, forwarded_ports=[3310])) == 3310
