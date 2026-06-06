"""PTY-based secure handshake engine.

Spawns native ssh in a pseudo-terminal, reads stdout/stderr byte-by-byte,
matches authentication/sudo prompts, and injects credentials directly into
the PTY buffer without environmental exposure.

Uses SecureCredential from src.security.memory for in-memory zeroing.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import termios
from typing import Callable

from ..security.ansi import strip_ansi
from ..security.memory import SecureCredential


# ---------------------------------------------------------------------------
# Prompt patterns
# ---------------------------------------------------------------------------

_AUTH_PROMPTS = [
    re.compile(r"password\s*:\s*$", re.IGNORECASE),
    re.compile(r"password\s*for\s+.+:\s*$", re.IGNORECASE),
    re.compile(r"\'?s password:\s*$", re.IGNORECASE),
]

_SUDO_PROMPTS = [
    re.compile(r"\[sudo\]\s+password\s+for\s+.+:\s*$", re.IGNORECASE),
    re.compile(r"sudo\s+password\s*:\s*$", re.IGNORECASE),
]

_HOST_KEY_PROMPTS = [
    re.compile(r"Are you sure you want to continue connecting", re.IGNORECASE),
]

_PRIVATE_KEY_PROMPTS = [
    re.compile(r"Enter passphrase for key", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Output callback type
# ---------------------------------------------------------------------------

OutputCallback = Callable[[str], None]  # receives sanitised text


# ---------------------------------------------------------------------------
# PTY runner
# ---------------------------------------------------------------------------


class PTYRunner:
    """Run a command in a PTY with interactive prompt handling.

    Credentials are wrapped in SecureCredential and zeroed after use.

    Usage:
        runner = PTYRunner()
        exit_code = runner.run(
            ["ssh", "-o", "StrictHostKeyChecking=yes", "user@host", "cmd"],
            password="secret",
            on_output=print,
        )
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._line_buffer = b""

    def run(
        self,
        argv: list[str],
        *,
        password: str = "",
        sudo_password: str = "",
        on_output: OutputCallback | None = None,
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> int:
        # Wrap credentials in SecureCredential for memory zeroing
        sec_password = SecureCredential(password) if password else None
        sec_sudo = SecureCredential(sudo_password) if sudo_password else None

        master_fd = -1
        pid = -1

        try:
            master_fd, slave_fd = pty.openpty()

            # Set non-blocking on master
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            pid = os.fork()
            if pid == 0:
                # Child
                os.close(master_fd)
                os.setsid()

                # Attach slave as stdin/stdout/stderr
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)

                exec_env = os.environ.copy()
                if env:
                    exec_env.update(env)

                os.execvpe(argv[0], argv, exec_env)

            # Parent
            os.close(slave_fd)

            return self._drive(
                master_fd, pid,
                sec_password, sec_sudo,
                on_output, timeout,
            )
        finally:
            # Zero credentials immediately after use
            if sec_password:
                sec_password.zero()
            if sec_sudo:
                sec_sudo.zero()
            try:
                if master_fd >= 0:
                    os.close(master_fd)
            except Exception:
                pass
            if pid > 0:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def _drive(
        self,
        master_fd: int,
        pid: int,
        password: SecureCredential | None,
        sudo_password: SecureCredential | None,
        on_output: OutputCallback | None,
        timeout: float,
    ) -> int:
        """Drive the PTY interaction loop."""
        import time

        start = time.monotonic()
        eof_count = 0
        sent_password = False
        sent_sudo = False

        # Get password bytes once (before potential zeroing)
        pw_bytes = password.value if password and not password.zeroed else b""
        sudo_bytes = sudo_password.value if sudo_password and not sudo_password.zeroed else b""

        while True:
            # Check if child is still alive
            try:
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid != 0:
                    self._drain_output(master_fd, on_output)
                    if os.WIFEXITED(status):
                        return os.WEXITSTATUS(status)
                    return 1
            except ChildProcessError:
                self._drain_output(master_fd, on_output)
                return 1

            # Timeout check
            if time.monotonic() - start > timeout:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                return 124

            # Read available data
            try:
                data = os.read(master_fd, 4096)
                if not data:
                    eof_count += 1
                    if eof_count > 3:
                        break
                    continue
                eof_count = 0
                self._buffer += data
            except (OSError, BlockingIOError):
                select.select([master_fd], [], [], 0.1)
                continue

            # Process complete lines
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                self._line_buffer = line
                text = strip_ansi(line)
                if on_output:
                    on_output(text)

                line_str = strip_ansi(line).rstrip()

                # Check for SSH host key prompt
                if any(p.search(line_str) for p in _HOST_KEY_PROMPTS):
                    os.write(master_fd, b"yes\n")
                    continue

                # Check for password prompts
                if any(p.search(line_str) for p in _AUTH_PROMPTS):
                    if pw_bytes and not sent_password:
                        os.write(master_fd, pw_bytes + b"\n")
                        sent_password = True
                    continue

                # Check for sudo prompts
                if any(p.search(line_str) for p in _SUDO_PROMPTS):
                    if sudo_bytes and not sent_sudo:
                        os.write(master_fd, sudo_bytes + b"\n")
                        sent_sudo = True
                    continue

                # Check for private key passphrase
                if any(p.search(line_str) for p in _PRIVATE_KEY_PROMPTS):
                    if pw_bytes and not sent_password:
                        os.write(master_fd, pw_bytes + b"\n")
                        sent_password = True
                    continue

        return 0

    def _drain_output(self, master_fd: int, on_output: OutputCallback | None) -> None:
        """Drain any remaining output from the PTY."""
        while True:
            try:
                data = os.read(master_fd, 4096)
                if not data:
                    break
                self._buffer += data
            except (OSError, BlockingIOError):
                break

        if self._buffer and on_output:
            text = strip_ansi(self._buffer)
            on_output(text)
            self._buffer = b""


# ---------------------------------------------------------------------------
# SSH command builder
# ---------------------------------------------------------------------------


def build_ssh_args(
    host: str,
    user: str = "",
    port: int = 22,
    key: str = "",
    strict_host_checking: bool = True,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build ssh command arguments."""
    args = ["ssh"]
    args.extend(["-o", "BatchMode=no"])
    args.extend(["-o", "ConnectTimeout=10"])

    if not strict_host_checking:
        args.extend(["-o", "StrictHostKeyChecking=no"])
    else:
        args.extend(["-o", "StrictHostKeyChecking=yes"])

    if port != 22:
        args.extend(["-p", str(port)])

    if key:
        args.extend(["-i", key])

    if extra_args:
        args.extend(extra_args)

    target = f"{user}@{host}" if user else host
    args.append(target)
    return args


def build_scp_args(
    src: str,
    dest: str,
    host: str,
    user: str = "",
    port: int = 22,
    key: str = "",
    recursive: bool = False,
) -> list[str]:
    """Build scp command arguments."""
    args = ["scp"]
    args.extend(["-o", "BatchMode=no"])
    args.extend(["-o", "ConnectTimeout=10"])
    args.extend(["-o", "StrictHostKeyChecking=yes"])

    if port != 22:
        args.extend(["-P", str(port)])

    if key:
        args.extend(["-i", key])

    if recursive:
        args.append("-r")

    args.extend([src, dest])
    return args
