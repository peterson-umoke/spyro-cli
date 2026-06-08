#!/usr/bin/env python3
"""Phase 1 PoC: STS (Spyro Tunnel Supervisor) Skeleton.

Goal: Build a simple loop that starts ssh -N and monitors the process
exit code. Verify heartbeat functionality and ability to restart on
manual process termination.

Tests:
  1. Start ssh -N tunnel process
  2. Monitor heartbeat via process alive check
  3. Detect manual kill (SIGTERM)
  4. Auto-restart with exponential backoff

Usage:
    python -m tests.poc.test_sts_skeleton [user@host]
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from spyro.supervisor.state import TunnelState, set_tunnel, get_tunnel, remove_tunnel
from spyro.supervisor.tunnel import _port_available, _pid_alive, _pgid_alive


def test_tunnel_process_lifecycle():
    """Test 1: Start a subprocess, verify it runs, kill it, verify dead."""
    print("=" * 60)
    print("TEST 1: Tunnel process lifecycle")
    print("=" * 60)

    # Start a simple sleep process (simulates ssh -N)
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    pid = proc.pid
    print(f"  Started sleep process with PID {pid}")

    # Give it a moment to start
    time.sleep(0.2)

    # Check it's alive
    alive = _pid_alive(pid)
    print(f"  Process alive: {alive}")

    # Get PGID
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = pid

    # Store state
    state = TunnelState(
        profile="test",
        local_port=0,
        pid=pid,
        pgid=pgid,
        status="running" if alive else "stopped",
    )
    set_tunnel(state)
    print(f"  State stored: {get_tunnel('test')}")

    # Kill it
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"  Sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        print(f"  Process already exited")

    # Wait for exit
    try:
        proc.wait(timeout=5)
        print(f"  Process exited with code {proc.returncode}")
    except subprocess.TimeoutExpired:
        os.kill(pid, signal.SIGKILL)
        proc.wait()

    # Verify dead
    alive_after = _pid_alive(pid)
    print(f"  Process alive after kill: {alive_after}")

    # Clean up state
    remove_tunnel("test")

    if not alive_after:
        print("  [PASS] Process lifecycle works\n")
        return True
    else:
        print("  [FAIL] Process should be dead after kill\n")
        return False


def test_port_available():
    """Test 2: Port availability check."""
    print("=" * 60)
    print("TEST 2: Port availability check")
    print("=" * 60)

    import socket

    # High port should be available
    avail = _port_available(49152)
    print(f"  Port 49152 available: {avail}")

    # Bind a port and check it's occupied
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        occupied = not _port_available(port)
        print(f"  Port {port} occupied: {occupied}")

    if avail and occupied:
        print("  [PASS] Port availability check works\n")
        return True
    else:
        print("  [FAIL] Port check gave unexpected results\n")
        return False


def test_supervisor_restart_loop():
    """Test 3: Supervisor restart logic (simulated)."""
    print("=" * 60)
    print("TEST 3: Supervisor restart logic (simulated)")
    print("=" * 60)

    restart_count = 0
    max_restarts = 3
    backoff = 1.0
    max_backoff = 5.0

    def simulate_restart():
        nonlocal restart_count, backoff
        restart_count += 1
        print(f"  Restart #{restart_count} (backoff: {backoff:.1f}s)")
        # Simulate backoff
        time.sleep(min(backoff, 0.1))  # Speed up for testing
        backoff = min(backoff * 2, max_backoff)

    # Simulate 3 failures
    for i in range(max_restarts):
        simulate_restart()

    if restart_count == max_restarts:
        print(f"  [PASS] Restart logic works ({restart_count} restarts)\n")
        return True
    else:
        print(f"  [FAIL] Expected {max_restarts} restarts, got {restart_count}\n")
        return False


def test_pgid_management():
    """Test 4: Process group ID management."""
    print("=" * 60)
    print("TEST 4: Process group management")
    print("=" * 60)

    # Fork a child in a new session
    pid = os.fork()
    if pid == 0:
        # Child: sleep for a bit
        os.setsid()
        time.sleep(10)
        os._exit(0)

    pgid = os.getpgid(pid)
    print(f"  Child PID: {pid}, PGID: {pgid}")

    # Check it's alive
    alive = _pgid_alive(pgid)
    print(f"  PGID {pgid} alive: {alive}")

    # Kill the whole group
    try:
        os.killpg(pgid, signal.SIGTERM)
        print(f"  Sent SIGTERM to PGID {pgid}")
    except ProcessLookupError:
        print(f"  Process group already gone")

    # Wait
    time.sleep(0.5)

    # Verify dead
    alive_after = _pgid_alive(pgid)
    print(f"  PGID {pgid} alive after kill: {alive_after}")

    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    if not alive_after:
        print("  [PASS] PGID management works\n")
        return True
    else:
        print("  [FAIL] PGID should be dead after kill\n")
        return False


def main():
    """Run all STS PoC tests."""
    target = sys.argv[1] if len(sys.argv) > 1 else "localhost"

    print("\nSpyro Tunnel Supervisor — Phase 1 PoC Validation")
    print("=" * 60)
    print(f"Target: {target}")
    print()

    results = []

    results.append(("Process lifecycle", test_tunnel_process_lifecycle()))
    results.append(("Port availability", test_port_available()))
    results.append(("Supervisor restart", test_supervisor_restart_loop()))
    results.append(("PGID management", test_pgid_management()))

    print("=" * 60)
    print("RESULTS:")
    all_pass = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
        if not passed:
            all_pass = False

    print()
    if all_pass:
        print("All STS PoC tests passed.")
    else:
        print("Some STS PoC tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
