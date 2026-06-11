"""PTY-based secure handshake engine.

Spawns native ssh in a pseudo-terminal, reads stdout/stderr byte-by-byte,
matches authentication/sudo prompts, and injects credentials directly into
the PTY buffer without environmental exposure.

Uses SecureCredential from spyro.security.memory for in-memory zeroing.
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

            # Process any remaining buffer data (prompts without trailing newline)
            if self._buffer:
                buf_str = strip_ansi(self._buffer).rstrip()
                if any(p.search(buf_str) for p in _AUTH_PROMPTS):
                    if pw_bytes and not sent_password:
                        os.write(master_fd, pw_bytes + b"\n")
                        sent_password = True
                        self._buffer = b""
                    continue
                if any(p.search(buf_str) for p in _SUDO_PROMPTS):
                    if sudo_bytes and not sent_sudo:
                        os.write(master_fd, sudo_bytes + b"\n")
                        sent_sudo = True
                        self._buffer = b""
                    continue
                if any(p.search(buf_str) for p in _HOST_KEY_PROMPTS):
                    os.write(master_fd, b"yes\n")
                    self._buffer = b""
                    continue

        return 0

    def interactive_run(
        self,
        argv: list[str],
        *,
        password: str = "",
        sudo_password: str = "",
        timeout: float = 30.0,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command interactively in a PTY.

        Handles the auth phase (password/sudo/host-key injection), then
        enters raw relay mode connecting the remote PTY to the user's terminal.
        Terminal is restored on exit even if interrupted.
        """
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
            return self._drive_interactive(
                master_fd, pid,
                sec_password, sec_sudo,
                timeout,
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

    def _drive_interactive(
        self,
        master_fd: int,
        pid: int,
        password: SecureCredential | None,
        sudo_password: SecureCredential | None,
        timeout: float,
    ) -> int:
        """Drive the PTY through auth, then relay raw between user terminal and remote."""
        import sys
        import time
        import termios as termios_mod

        fd_stdin = -1
        fd_stdout = sys.stdout.fileno()

        # Determine usable stdin fd (pytest captures stdin — fall back to /dev/null)
        try:
            fd_stdin = sys.stdin.fileno()
        except (OSError, Exception):
            try:
                fd_stdin = os.open("/dev/null", os.O_RDONLY)
            except OSError:
                fd_stdin = -1

        start = time.monotonic()
        sent_password = False
        sent_sudo = False
        auth_done = False
        # Track whether we've received non-prompt output from the remote.
        # If the shell is already producing output (MOTD, etc.) and no auth
        # prompt has appeared, SSH key-based auth succeeded — don't wait
        # for a password prompt that's never coming.
        _received_output = False

        pw_bytes = password.value if password and not password.zeroed else b""
        sudo_bytes = sudo_password.value if sudo_password and not sudo_password.zeroed else b""

        old_attr = None
        try:
            if os.isatty(fd_stdin):
                old_attr = termios_mod.tcgetattr(fd_stdin)
                # Set raw mode so Ctrl+C, arrows, etc. pass through to the remote
                import tty as tty_mod
                tty_mod.setraw(fd_stdin)
        except Exception:
            pass

        try:
            while True:
                # Check child status
                try:
                    wpid, wstatus = os.waitpid(pid, os.WNOHANG)
                    if wpid != 0:
                        if os.WIFEXITED(wstatus):
                            return os.WEXITSTATUS(wstatus)
                        return 1
                except ChildProcessError:
                    return 1

                # Timeout for auth phase only
                if not auth_done and time.monotonic() - start > timeout:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                    return 124

                # Read from PTY
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        # EOF from remote — shell exited
                        break

                    if not auth_done:
                        self._buffer += data
                        # Process prompts in buffer
                        while b"\n" in self._buffer:
                            line, rest = self._buffer.split(b"\n", 1)
                            self._buffer = rest
                            text = strip_ansi(line)
                            line_str = text.rstrip()

                            if any(p.search(line_str) for p in _HOST_KEY_PROMPTS):
                                os.write(master_fd, b"yes\n")
                                continue
                            if any(p.search(line_str) for p in _AUTH_PROMPTS):
                                if pw_bytes and not sent_password:
                                    os.write(master_fd, pw_bytes + b"\n")
                                    sent_password = True
                                continue
                            if any(p.search(line_str) for p in _SUDO_PROMPTS):
                                if sudo_bytes and not sent_sudo:
                                    os.write(master_fd, sudo_bytes + b"\n")
                                    sent_sudo = True
                                continue
                            if any(p.search(line_str) for p in _PRIVATE_KEY_PROMPTS):
                                if pw_bytes and not sent_password:
                                    os.write(master_fd, pw_bytes + b"\n")
                                    sent_password = True
                                continue

                            # Not a prompt — print to user
                            os.write(fd_stdout, text.encode() + b"\n")
                            os.fsync(fd_stdout)
                            _received_output = True

                        # Check remaining buffer for prompt fragments
                        if self._buffer:
                            buf_str = strip_ansi(self._buffer).rstrip()
                            if any(p.search(buf_str) for p in _AUTH_PROMPTS):
                                if pw_bytes and not sent_password:
                                    os.write(master_fd, pw_bytes + b"\n")
                                    sent_password = True
                                    self._buffer = b""
                                    continue
                            if any(p.search(buf_str) for p in _SUDO_PROMPTS):
                                if sudo_bytes and not sent_sudo:
                                    os.write(master_fd, sudo_bytes + b"\n")
                                    sent_sudo = True
                                    self._buffer = b""
                                    continue
                            if any(p.search(buf_str) for p in _HOST_KEY_PROMPTS):
                                os.write(master_fd, b"yes\n")
                                self._buffer = b""
                                continue

                        # Detect auth phase complete
                        has_auth = bool(pw_bytes)
                        has_sudo = bool(sudo_bytes)
                        # If the remote already sent shell output (MOTD, prompt,
                        # etc.) without us ever seeing a password prompt, key-based
                        # SSH auth succeeded — don't wait for a prompt that's
                        # never coming.
                        auto_auth = _received_output and not sent_password and not sent_sudo
                        if (not has_auth or sent_password or auto_auth) and (not has_sudo or sent_sudo or auto_auth):
                            auth_done = True
                            # Flush buffered output
                            remaining = strip_ansi(self._buffer)
                            if remaining.strip():
                                os.write(fd_stdout, remaining.encode())
                                os.fsync(fd_stdout)
                            self._buffer = b""
                    else:
                        # Raw relay: PTY → user stdout
                        os.write(fd_stdout, data)
                        os.fsync(fd_stdout)

                except (OSError, BlockingIOError):
                    pass

                # Forward user stdin → PTY (after auth)
                if auth_done:
                    try:
                        rlist, _, _ = select.select([fd_stdin], [], [], 0.05)
                        if rlist:
                            input_data = os.read(fd_stdin, 4096)
                            if not input_data:
                                # EOF from user (Ctrl+D or close)
                                break
                            # Handle SSH escape sequences we want to pass through
                            os.write(master_fd, input_data)
                    except (OSError, BlockingIOError):
                        pass
                else:
                    select.select([master_fd], [], [], 0.05)

            # EOF from remote or stdin — shell exited
            return 0

        finally:
            if old_attr and os.isatty(fd_stdin):
                try:
                    termios_mod.tcsetattr(fd_stdin, termios_mod.TCSADRAIN, old_attr)
                except Exception:
                    pass

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
    import os

    args = ["ssh"]
    args.extend(["-o", "BatchMode=no"])
    args.extend(["-o", "ConnectTimeout=10"])

    # Enable connection sharing to speed up multiple SSH calls to the same host
    # Use ~/.spyro/sockets/ instead of OS tempdir — macOS has a 104-char Unix socket
    # path limit, and tempdir on macOS (e.g. /var/folders/99/.../T/) burns ~51 chars
    # before we even start. SSH appends a random suffix to ControlPath, so a path
    # that looks fine can still exceed the limit (see the original bug report).
    sock_dir = os.path.join(os.path.expanduser("~"), ".spyro", "sockets")
    os.makedirs(sock_dir, exist_ok=True)
    ctrl_path = os.path.join(sock_dir, f"s-{user or 'anon'}@{host}:{port}")
    args.extend(["-o", f"ControlPath={ctrl_path}"])
    args.extend(["-o", "ControlMaster=auto"])
    args.extend(["-o", "ControlPersist=15s"])

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


def _scp_target(path: str, host: str, user: str = "") -> str:
    """Prepend user@host to a remote SCP path, ignoring any profile: or : prefix."""
    # Strip leading ':' (remote marker) or 'profile:' prefix
    if path.startswith(":"):
        path = path.lstrip(":")
    if ":" in path and not path.startswith("/"):
        _, path = path.split(":", 1)
    prefix = f"{user}@{host}" if user else host
    return f"{prefix}:{path}"


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
    args.extend(["-o", "StrictHostKeyChecking=accept-new"])

    if port != 22:
        args.extend(["-P", str(port)])

    if key:
        args.extend(["-i", key])

    if recursive:
        args.append("-r")

    args.extend([src, dest])
    return args
