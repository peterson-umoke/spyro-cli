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
import shutil
import signal
import struct
import termios
import time
from typing import Callable

from ..security.ansi import strip_ansi
from ..security.memory import SecureCredential
from ..utils.paths import sockets_dir


# ---------------------------------------------------------------------------
# Prompt patterns
# ---------------------------------------------------------------------------

# Anchored to the start of the line so that a *remote program's* prompt
# ("Enter password:", "mysql> password:") never receives the SSH password.
_AUTH_PROMPTS = [
    re.compile(r"[^\s@]+@\S+'s password\s*:\s*$", re.IGNORECASE),  # user@host's password: (may follow banner text)
    re.compile(r"^(?:\([^)]*\)\s*)?password\s*:\s*$", re.IGNORECASE),  # Password: / (u@h) Password:
    re.compile(r"^password\s+for\s+\S+\s*:\s*$", re.IGNORECASE),  # Password for u@h:
]

_SUDO_PROMPTS = [
    # sudo normally emits "[sudo] password for <user>: ", but the exact
    # spacing and whether the user name is included can vary by platform.
    re.compile(r"\[\s*sudo\s*\]\s*password(?:\s+for\s+[^:]+)?\s*:\s*$", re.IGNORECASE),
    re.compile(r"sudo\s+password(?:\s+for\s+[^:]+)?\s*:\s*$", re.IGNORECASE),
]

_HOST_KEY_PROMPTS = [
    re.compile(r"Are you sure you want to continue connecting", re.IGNORECASE),
]

_PRIVATE_KEY_PROMPTS = [
    re.compile(r"Enter passphrase for key", re.IGNORECASE),
]

_NO_PASSWORD_MSG = (
    "spyro: SSH is asking for a password but none is stored. "
    "Run: spyro auth set -p <profile>"
)
_REJECTED_MSG = (
    "spyro: SSH rejected the stored password. "
    "Update it with: spyro auth set -p <profile> -f"
)


def _matches(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


# ---------------------------------------------------------------------------
# Output callback type
# ---------------------------------------------------------------------------

OutputCallback = Callable[[str], None]  # receives sanitised text


# ---------------------------------------------------------------------------
# PTY helpers
# ---------------------------------------------------------------------------


def _set_winsize(fd: int) -> None:
    """Give the PTY the local terminal's size (80x24 when there is none)."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0))
    except OSError:
        pass


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def _spawn(argv: list[str], env: dict[str, str] | None) -> tuple[int, int]:
    """Fork *argv* onto a fresh PTY. Returns ``(master_fd, pid)``."""
    master_fd, slave_fd = pty.openpty()
    try:
        _set_winsize(slave_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        pid = os.fork()
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise

    if pid == 0:
        # Child. It must never return into the caller's code: if exec fails
        # it would keep running the CLI (and its finally blocks) as a clone.
        try:
            os.close(master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for fd in (0, 1, 2):
                os.dup2(slave_fd, fd)
            if slave_fd > 2:
                os.close(slave_fd)
            exec_env = os.environ.copy()
            if env:
                exec_env.update(env)
            os.execvpe(argv[0], argv, exec_env)
        except BaseException as e:
            try:
                os.write(2, f"spyro: cannot run {argv[0]}: {getattr(e, 'strerror', None) or e}\n".encode())
            except OSError:
                pass
        finally:
            os._exit(127)

    os.close(slave_fd)
    return master_fd, pid


def _exit_status(pid: int, wait: float = 2.0) -> int | None:
    """Exit code of *pid*, waiting up to *wait* seconds for it to finish.

    After the PTY reports EOF the child may not be reapable for a few
    milliseconds; returning 0 there would report a failed command as success.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if wpid != 0:
            return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _reap(master_fd: int, pid: int) -> None:
    try:
        if master_fd >= 0:
            os.close(master_fd)
    except OSError:
        pass
    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


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
        timeout: float | None = 30.0,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run *argv*, answering prompts. ``timeout=None`` waits indefinitely.

        Returns the exit code; 124 on timeout, 255 when SSH asks for a password
        that is missing or was rejected, 127 when the program cannot be run.
        """
        # Clear instance buffers (runner may be reused across profiles)
        self._buffer = b""
        self._line_buffer = b""

        # Wrap credentials in SecureCredential for memory zeroing
        sec_password = SecureCredential(password) if password else None
        sec_sudo = SecureCredential(sudo_password) if sudo_password else None

        master_fd = -1
        pid = -1

        try:
            master_fd, pid = _spawn(argv, env)
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
            _reap(master_fd, pid)

    def _drive(
        self,
        master_fd: int,
        pid: int,
        password: SecureCredential | None,
        sudo_password: SecureCredential | None,
        on_output: OutputCallback | None,
        timeout: float | None,
    ) -> int:
        """Drive the PTY interaction loop."""
        start = time.monotonic()
        eof_count = 0
        sent_password = False
        sudo_attempts = 0
        max_sudo_attempts = 6
        partial_since: float | None = None
        # Get password bytes once (before potential zeroing)
        pw_bytes = password.value if password and not password.zeroed else b""
        sudo_bytes = sudo_password.value if sudo_password and not sudo_password.zeroed else b""

        notes: list[str] = []

        def answer(text: str) -> bool | int:
            """React to a prompt in *text*.

            Returns False when *text* is not a prompt, True when it was
            answered, or an exit code when the child had to be aborted.
            """
            nonlocal sent_password, sudo_attempts
            if _matches(_HOST_KEY_PROMPTS, text):
                os.write(master_fd, b"yes\n")
                return True
            # Sudo BEFORE generic auth: a looser "password for user:" pattern
            # would also match inside "[sudo] password for user: ".
            if _matches(_SUDO_PROMPTS, text):
                if not sudo_bytes or sudo_attempts >= max_sudo_attempts:
                    return self._abort_child(pid, 1)
                os.write(master_fd, sudo_bytes + b"\n")
                sudo_attempts += 1
                return True
            if _matches(_AUTH_PROMPTS, text) or _matches(_PRIVATE_KEY_PROMPTS, text):
                if sent_password and sudo_bytes and sudo_attempts < 2:
                    # A second plain "Password:" after a successful login is how
                    # BSD/macOS sudo prompts; the profile has one password for both.
                    os.write(master_fd, sudo_bytes + b"\n")
                    sudo_attempts += 1
                    return True
                if not pw_bytes or sent_password:
                    # Nothing (more) to send: fail now instead of idling
                    # until the timeout.
                    notes.append(_REJECTED_MSG if sent_password else _NO_PASSWORD_MSG)
                    return self._abort_child(pid, 255)
                os.write(master_fd, pw_bytes + b"\n")
                sent_password = True
                return True
            return False

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
            if timeout is not None and time.monotonic() - start > timeout:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                return 124

            # Read available data
            try:
                data = os.read(master_fd, 4096)
                if not data:
                    # EOF: the child closed its side. Its exit status is
                    # picked up by the waitpid at the top of the loop.
                    eof_count += 1
                    if eof_count > 300:  # ~3s: output closed but the child lives on
                        break
                    time.sleep(0.01)
                    continue
                eof_count = 0
                if not self._buffer:
                    partial_since = time.monotonic()
                self._buffer += data
            except (OSError, BlockingIOError):
                select.select([master_fd], [], [], 0.1)
                if not self._buffer:
                    continue

            # Process complete lines
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                self._line_buffer = line
                text = strip_ansi(line)
                if on_output:
                    on_output(text)
                result = answer(text.rstrip())
                if result is not True and result is not False:
                    if on_output:
                        for note in notes:
                            on_output(note)
                    return result

            if not self._buffer:
                partial_since = None
                continue

            # Process remaining data.  Prompt text is commonly emitted
            # without a newline, so retain partial chunks long enough for a
            # split prompt to arrive.  If it is still unmatched after a
            # short idle period, emit it instead of hiding it indefinitely.
            buf_str = strip_ansi(self._buffer).rstrip()
            result = answer(buf_str)
            if result is True:
                self._buffer = b""
                partial_since = None
            elif result is not False:
                if on_output:
                    on_output(strip_ansi(self._buffer))
                    for note in notes:
                        on_output(note)
                return result
            elif partial_since is not None and time.monotonic() - partial_since >= 0.5:
                if on_output:
                    on_output(strip_ansi(self._buffer))
                self._buffer = b""
                partial_since = None

        return 0

    @staticmethod
    def _abort_child(pid: int, exit_code: int) -> int:
        """Terminate a child after an unrecoverable prompt interaction."""
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return exit_code

    def interactive_run(
        self,
        argv: list[str],
        *,
        password: str = "",
        sudo_password: str = "",
        timeout: float | None = 30.0,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run a command interactively in a PTY.

        Handles the auth phase (password/sudo/host-key injection), then
        enters raw relay mode connecting the remote PTY to the user's terminal.
        Terminal is restored on exit even if interrupted. *timeout* bounds the
        auth phase only.
        """
        self._buffer = b""
        self._line_buffer = b""

        sec_password = SecureCredential(password) if password else None
        sec_sudo = SecureCredential(sudo_password) if sudo_password else None

        master_fd = -1
        pid = -1

        try:
            master_fd, pid = _spawn(argv, env)
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
            _reap(master_fd, pid)

    def _drive_interactive(
        self,
        master_fd: int,
        pid: int,
        password: SecureCredential | None,
        sudo_password: SecureCredential | None,
        timeout: float | None,
    ) -> int:
        """Drive the PTY through auth, then relay raw between user terminal and remote."""
        import sys
        import tty

        fd_stdout = sys.stdout.fileno()

        # Determine usable stdin fd (pytest captures stdin — fall back to /dev/null)
        try:
            fd_stdin = sys.stdin.fileno()
        except (OSError, ValueError):
            try:
                fd_stdin = os.open("/dev/null", os.O_RDONLY)
            except OSError:
                fd_stdin = -1

        start = time.monotonic()
        last_data = start
        sent_password = False
        sent_sudo = False
        auth_done = False
        # Track whether we've received non-prompt output from the remote.
        # If the shell is already producing output (MOTD, etc.) and no auth
        # prompt has appeared, SSH key-based auth succeeded — don't wait
        # for a password prompt that's never coming.
        _received_output = False
        got_data = False
        stdin_open = True

        pw_bytes = password.value if password and not password.zeroed else b""
        sudo_bytes = sudo_password.value if sudo_password and not sudo_password.zeroed else b""
        has_auth = bool(pw_bytes)
        has_sudo = bool(sudo_bytes)

        def answer(text: str) -> bool:
            nonlocal sent_password, sent_sudo
            if _matches(_HOST_KEY_PROMPTS, text):
                os.write(master_fd, b"yes\n")
                return True
            # Sudo before generic auth (see _drive).
            if _matches(_SUDO_PROMPTS, text):
                if sudo_bytes and not sent_sudo:
                    os.write(master_fd, sudo_bytes + b"\n")
                    sent_sudo = True
                return True
            if _matches(_AUTH_PROMPTS, text) or _matches(_PRIVATE_KEY_PROMPTS, text):
                if pw_bytes and not sent_password:
                    os.write(master_fd, pw_bytes + b"\n")
                    sent_password = True
                return True
            return False

        old_attr = None
        try:
            if os.isatty(fd_stdin):
                old_attr = termios.tcgetattr(fd_stdin)
                # Set raw mode so Ctrl+C, arrows, etc. pass through to the remote
                tty.setraw(fd_stdin)
        except Exception:
            pass

        # Keep the remote terminal size in sync with ours.
        old_winch = None
        try:
            _set_winsize(master_fd)
            old_winch = signal.signal(signal.SIGWINCH, lambda *_: _set_winsize(master_fd))
        except (ValueError, OSError):  # not the main thread
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
                if not auth_done and timeout is not None and time.monotonic() - start > timeout:
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
                        status = _exit_status(pid)
                        return 0 if status is None else status
                    got_data = True
                    last_data = time.monotonic()

                    if not auth_done:
                        self._buffer += data
                        # Process prompts in buffer
                        while b"\n" in self._buffer:
                            line, self._buffer = self._buffer.split(b"\n", 1)
                            text = strip_ansi(line)
                            if answer(text.rstrip()):
                                continue
                            # Not a prompt — print to user
                            _write_all(fd_stdout, text.encode() + b"\n")
                            _received_output = True

                        # Check remaining buffer for prompt fragments
                        if self._buffer and answer(strip_ansi(self._buffer).rstrip()):
                            self._buffer = b""
                    else:
                        # Raw relay: PTY → user stdout
                        _write_all(fd_stdout, data)

                except (OSError, BlockingIOError):
                    pass

                # Detect auth phase complete.
                if not auth_done and got_data:
                    # "Quiet" = the remote showed us something (MOTD, a shell
                    # prompt) and then went silent. A password or sudo prompt
                    # follows the connection within milliseconds, so this means
                    # no more prompts are coming: key auth worked, or it is
                    # just a shell waiting for input.
                    quiet = time.monotonic() - last_data > 1.0 and (_received_output or bool(self._buffer))
                    # Answering a sudo prompt proves the login already succeeded.
                    logged_in = not has_auth or sent_password or sent_sudo or quiet
                    sudo_done = not has_sudo or sent_sudo or quiet
                    if logged_in and sudo_done:
                        auth_done = True
                        # Flush buffered output
                        remaining = strip_ansi(self._buffer)
                        if remaining.strip():
                            _write_all(fd_stdout, remaining.encode())
                        self._buffer = b""

                # Forward user stdin → PTY (after auth)
                if auth_done:
                    try:
                        watch = [master_fd] + ([fd_stdin] if stdin_open else [])
                        rlist, _, _ = select.select(watch, [], [], 0.05)
                        if stdin_open and fd_stdin in rlist:
                            input_data = os.read(fd_stdin, 4096)
                            if not input_data:
                                # EOF from the user (piped input ran out): send
                                # Ctrl+D like a terminal would, but keep relaying
                                # until the remote finishes, so its output for
                                # what we just sent is not thrown away.
                                _write_all(master_fd, b"\x04")
                                stdin_open = False
                            else:
                                _write_all(master_fd, input_data)
                    except (OSError, BlockingIOError, ValueError):
                        pass
                else:
                    select.select([master_fd], [], [], 0.05)

            # EOF from remote or stdin — shell exited
            return 0

        finally:
            if old_winch is not None:
                signal.signal(signal.SIGWINCH, old_winch)
            if old_attr and os.isatty(fd_stdin):
                try:
                    termios.tcsetattr(fd_stdin, termios.TCSADRAIN, old_attr)
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
    """Build ssh command arguments.

    *strict_host_checking* uses ``accept-new``: unknown hosts are trusted on
    first use, a *changed* host key is still refused. (Plain ``yes`` refuses
    every first connection without prompting, which broke fresh setups.)
    """
    args = ["ssh"]
    args.extend(["-o", "BatchMode=no"])
    args.extend(["-o", "ConnectTimeout=10"])

    # Enable connection sharing to speed up multiple SSH calls to the same host.
    # ``%C`` is a fixed-length (40 hex) hash of local host + remote host + port
    # + user, so the socket path never depends on how long the user/host names
    # are; sockets_dir() keeps the directory part short enough for macOS's
    # 104-byte Unix socket path limit.
    args.extend(["-o", f"ControlPath={sockets_dir()}/%C"])
    args.extend(["-o", "ControlMaster=auto"])
    args.extend(["-o", "ControlPersist=15s"])

    if not strict_host_checking:
        args.extend(["-o", "StrictHostKeyChecking=no"])
    else:
        args.extend(["-o", "StrictHostKeyChecking=accept-new"])

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
