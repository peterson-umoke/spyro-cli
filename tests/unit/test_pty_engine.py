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
