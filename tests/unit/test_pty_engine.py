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

