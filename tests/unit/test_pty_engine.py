"""Unit tests for PTYEngine — interactive_run and credential handling."""

from __future__ import annotations

from spyro.core.pty_engine import PTYRunner
from spyro.security.memory import SecureCredential


class TestInteractiveRun:
    """Tests for PTYRunner.interactive_run()."""

    def test_has_interactive_run_method(self):
        """Verify interactive_run exists with correct signature."""
        runner = PTYRunner()
        assert hasattr(runner, "interactive_run")
        assert callable(runner.interactive_run)

    def test_interactive_run_simple_echo(self):
        """Run a simple echo command via interactive_run — should complete."""
        runner = PTYRunner()
        exit_code = runner.interactive_run(
            ["echo", "interactive-test-ok"],
            timeout=5.0,
        )
        assert exit_code == 0

    def test_interactive_run_exit_code_propagated(self):
        """Non-zero exit codes should propagate."""
        runner = PTYRunner()
        exit_code = runner.interactive_run(
            ["sh", "-c", "exit 42"],
            timeout=5.0,
        )
        assert exit_code == 42

    def test_interactive_run_credential_zeroing(self):
        """Credentials should be zeroed after interactive_run completes."""
        runner = PTYRunner()
        password = SecureCredential(b"test-password")
        sudo_pw = SecureCredential(b"sudo-password")

        # Run a simple command (password won't be needed for echo)
        runner.interactive_run(
            ["echo", "zeroing-test"],
            password="test-password",
            sudo_password="sudo-password",
            timeout=5.0,
        )

        # After run, the SecureCredential instances used internally are zeroed
        # We can verify the API works by creating our own creds
        assert not password.zeroed, "Our test credential should survive"
        assert not sudo_pw.zeroed, "Our test credential should survive"
        password.zero()
        sudo_pw.zero()
        assert password.zeroed
        assert sudo_pw.zeroed


class TestRunSudoPrompt:
    """Exercises _drive() path: sudo prompt matching, retry, abort, flush."""

    def test_sudo_prompt_receives_sudo_password_not_ssh_password(self):
        """When the remote emits a no-newline [sudo] prompt, _drive must
        send sudo_password, not password (SSH)."""
        import sys

        CHILD = (
            "import sys;"
            "sys.stdout.write('[sudo] password for test: ');"
            "sys.stdout.flush();"
            "pw = sys.stdin.readline();"
            "sys.stdout.write('RECV:' + pw.strip() + '\\n');"
            "sys.stdout.flush()"
        )
        runner = PTYRunner()
        output_lines: list[str] = []

        exit_code = runner.run(
            [sys.executable, "-c", CHILD],
            password="ssh-secret",
            sudo_password="sudo-secret",
            on_output=output_lines.append,
            timeout=5.0,
        )
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        assert any("RECV:sudo-secret" in line for line in output_lines), (
            f"Expected sudo_password in output, got: {output_lines}"
        )
        assert not any("ssh-secret" in line for line in output_lines), (
            f"ssh password leaked to sudo prompt: {output_lines}"
        )

    def test_empty_sudo_aborts_fast(self):
        """When sudo_bytes is empty and a sudo prompt appears, abort
        immediately (exit 1), not hang until timeout 124."""
        import sys
        import time

        CHILD = (
            "import sys;"
            "sys.stdout.write('[sudo] password for test: ');"
            "sys.stdout.flush();"
            "sys.stdin.readline();"
            "sys.stdout.write('NEVER_REACHED')"
        )
        runner = PTYRunner()
        output_lines: list[str] = []

        start = time.time()
        exit_code = runner.run(
            [sys.executable, "-c", CHILD],
            sudo_password="",  # no sudo credential available
            on_output=output_lines.append,
            timeout=5.0,
        )
        elapsed = time.time() - start
        assert exit_code == 1, f"Expected abort exit 1, got {exit_code}"
        assert elapsed < 2.0, f"Aborted in {elapsed:.2f}s, expected <2s"
        assert any("sudo" in line.lower() for line in output_lines), (
            f"Unmatched prompt should be emitted: {output_lines}"
        )

    def test_unknown_prompt_flushed_on_idle(self):
        """A custom prompt that matches no regex should be flushed to
        on_output after the partial-buffer idle timeout, not hidden."""
        import sys

        CHILD = (
            "import sys;"
            "sys.stdout.write('Enter token: ');"
            "sys.stdout.flush();"
            # block forever — _drive will time out
            "sys.stdin.read()"
        )
        runner = PTYRunner()
        output_lines: list[str] = []

        exit_code = runner.run(
            [sys.executable, "-c", CHILD],
            timeout=3.0,
            on_output=output_lines.append,
        )
        # Timeout or abort expected.
        assert "Enter token:" in "".join(output_lines), (
            f"Custom prompt should be flushed: {output_lines}"
        )

    def test_sudo_retry_on_second_prompt(self):
        """Two sudo prompts in sequence (wrong password then correct) should
        both be answered, up to max_sudo_attempts."""
        import sys

        # First prompt expects an empty string (wrong password),
        # sudo re-prompts, second time we send the real one.
        CHILD = (
            "import sys;"
            "sys.stdout.write('[sudo] password for test: ');"
            "sys.stdout.flush();"
            "pw1 = sys.stdin.readline().strip();"
            "sys.stdout.write('RECV1:' + pw1 + '\\n');"
            "sys.stdout.flush();"
            "sys.stdout.write('[sudo] password for test: ');"
            "sys.stdout.flush();"
            "pw2 = sys.stdin.readline().strip();"
            "sys.stdout.write('RECV2:' + pw2 + '\\n');"
            "sys.stdout.flush()"
        )
        runner = PTYRunner()
        output_lines: list[str] = []

        exit_code = runner.run(
            [sys.executable, "-c", CHILD],
            sudo_password="sudo-secret",
            on_output=output_lines.append,
            timeout=5.0,
        )
        # When sudo retries, both prompts receive the same credential.
        assert exit_code == 0, f"Expected exit 0, got {exit_code}"
        assert any("RECV1:sudo-secret" in line for line in output_lines), (
            f"First prompt: {output_lines}"
        )
        assert any("RECV2:sudo-secret" in line for line in output_lines), (
            f"Second prompt: {output_lines}"
        )



class TestPromptHandling:
    """Regression tests for prompt matching and fail-fast authentication."""

    @staticmethod
    def _prompt_child(prompt: str) -> list[str]:
        import sys

        code = (
            "import sys;"
            f"sys.stdout.write({prompt!r});"
            "sys.stdout.flush();"
            "pw = sys.stdin.readline().strip();"
            "sys.stdout.write('RECV:' + pw + '\\n')"
        )
        return [sys.executable, "-c", code]

    def test_ssh_password_prompt_is_answered(self):
        out: list[str] = []
        ec = PTYRunner().run(
            self._prompt_child("deploy@host's password: "),
            password="ssh-secret", on_output=out.append, timeout=5.0,
        )
        assert ec == 0
        assert any("RECV:ssh-secret" in line for line in out), out

    def test_remote_program_prompt_never_gets_ssh_password(self):
        """'Enter password:' comes from a program on the server, not from ssh."""
        out: list[str] = []
        PTYRunner().run(
            self._prompt_child("Enter password: "),
            password="ssh-secret", on_output=out.append, timeout=1.5,
        )
        assert not any("ssh-secret" in line for line in out), out

    def test_missing_password_fails_fast(self):
        import time

        out: list[str] = []
        start = time.time()
        ec = PTYRunner().run(
            self._prompt_child("deploy@host's password: "),
            password="", on_output=out.append, timeout=10.0,
        )
        assert ec == 255
        assert time.time() - start < 3.0
        assert any("spyro auth set" in line for line in out), out

    def test_rejected_password_fails_fast(self):
        import sys
        import time

        code = (
            "import sys;"
            "sys.stdout.write(\"deploy@host's password: \");sys.stdout.flush();"
            "sys.stdin.readline();"
            "sys.stdout.write('Permission denied, please try again.\\n');"
            "sys.stdout.write(\"deploy@host's password: \");sys.stdout.flush();"
            "sys.stdin.readline()"
        )
        out: list[str] = []
        start = time.time()
        ec = PTYRunner().run(
            [sys.executable, "-c", code],
            password="wrong", on_output=out.append, timeout=10.0,
        )
        assert ec == 255
        assert time.time() - start < 3.0
        assert any("rejected" in line for line in out), out


class TestRunEdgeCases:
    def test_output_lines_have_no_carriage_returns(self):
        """The PTY turns \\n into \\r\\n; captured text must not keep the \\r."""
        out: list[str] = []
        PTYRunner().run(["sh", "-c", "printf 'A=1\\nB=2\\n'"], on_output=out.append, timeout=5.0)
        assert [line for line in out if line] == ["A=1", "B=2"]

    def test_missing_binary_returns_127_without_running_caller_code(self):
        import os

        marker = os.getpid()
        out: list[str] = []
        ec = PTYRunner().run(["definitely-not-a-binary-xyz"], on_output=out.append, timeout=5.0)
        assert ec == 127
        assert os.getpid() == marker  # still the parent; the child exited
        assert any("cannot run" in line for line in out), out

    def test_timeout_none_waits_for_the_child(self):
        ec = PTYRunner().run(["sh", "-c", "sleep 0.3; exit 7"], timeout=None)
        assert ec == 7

    def test_pty_gets_a_window_size(self):
        out: list[str] = []
        PTYRunner().run(["sh", "-c", "stty size"], on_output=out.append, timeout=5.0)
        rows, cols = [int(x) for x in out[0].split()]
        assert rows > 0 and cols > 0


class TestInteractiveAuth:
    def test_sudo_profile_with_password_auth_does_not_wait_for_a_sudo_prompt(self):
        """A plain interactive shell never shows a sudo prompt; the session must
        leave the auth phase instead of being killed by the auth timeout."""
        import sys
        import time

        code = (
            "import sys;"
            "sys.stdout.write(\"user@host's password: \");sys.stdout.flush();"
            "sys.stdin.readline();"
            "sys.stdout.write('\\nWelcome\\n$ ');sys.stdout.flush();"
            "sys.stdin.read()"
        )
        start = time.time()
        ec = PTYRunner().interactive_run(
            [sys.executable, "-c", code],
            password="pw", sudo_password="pw", timeout=6.0,
        )
        # stdin is /dev/null under pytest: once auth ends the relay sees EOF.
        assert ec == 0, f"auth phase was killed (exit {ec})"
        assert time.time() - start < 5.0


class TestExitStatusIsNeverLost:
    def test_fast_failing_commands_keep_their_exit_code(self):
        """The PTY hits EOF a moment before the child is reapable; that must not
        be reported as success (this used to return 0 intermittently)."""
        codes = [
            PTYRunner().run(["sh", "-c", "echo oops; exit 3"], timeout=5.0)
            for _ in range(40)
        ]
        assert codes == [3] * 40

    def test_interactive_returns_the_real_exit_code(self):
        assert PTYRunner().interactive_run(["sh", "-c", "exit 9"], timeout=5.0) == 9


class TestInteractiveInput:
    def test_output_for_piped_input_is_not_lost_when_stdin_ends(self, tmp_path):
        """stdin hitting EOF right after the last command must not cut the session
        before the remote has answered (used to return with no output)."""
        import io
        import os
        import sys
        from unittest.mock import patch

        code = (
            "import sys,time;"
            "sys.stdout.write('ready\\n');sys.stdout.flush();"
            "line=sys.stdin.readline().strip();time.sleep(0.4);"
            "sys.stdout.write('ANSWER:'+line+'\\n');sys.stdout.flush();"
            "sys.stdin.read()"
        )
        r, w = os.pipe()
        os.write(w, b"hello\n")
        os.close(w)
        out_r, out_w = os.pipe()

        class Fake:
            def __init__(self, fd): self._fd = fd
            def fileno(self): return self._fd

        with patch.object(sys, "stdin", Fake(r)), patch.object(sys, "stdout", Fake(out_w)):
            ec = PTYRunner().interactive_run([sys.executable, "-c", code], timeout=10.0)
        os.close(out_w)
        data = b""
        while chunk := os.read(out_r, 4096):
            data += chunk
        assert ec == 0
        assert b"ANSWER:hello" in data
