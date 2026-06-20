"""Tests for spyro cp command — path resolution."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from spyro.cli.commands import cmd_cp


@patch("spyro.utils.keychain.prompt_for_credential", return_value="test-pass")
@patch("spyro.cli.commands.PTYRunner")
@patch("spyro.cli.commands.load_config")
class TestCmdCpPathResolution:
    """Verify relative local paths are resolved to absolute before SCP."""

    def test_relative_src_resolved_to_absolute(self, mock_load_config, mock_runner_cls, _mock_pw):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.host = "staging.example.com"
        mock_profile.user = "deploy"
        mock_profile.port = 22
        mock_profile.key = ""
        mock_config.get_profile.return_value = mock_profile
        mock_load_config.return_value = mock_config

        mock_runner = MagicMock()
        mock_runner.run.return_value = 0
        mock_runner_cls.return_value = mock_runner

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("app").mkdir()
            (Path("app") / "test.txt").write_text("hello")

            result = runner.invoke(
                cmd_cp,
                ["./app", "/var/www/app", "-p", "staging"],
            )

            assert result.exit_code == 0, result.output

            scp_args = mock_runner.run.call_args[0][0]
            local_src = scp_args[-2]
            assert Path(local_src).is_absolute(), (
                f"src should be absolute, got: {local_src}"
            )
            assert local_src == str(Path("./app").resolve())

    def test_absolute_src_unmodified(self, mock_load_config, mock_runner_cls, _mock_pw):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.host = "staging.example.com"
        mock_profile.user = "deploy"
        mock_profile.port = 22
        mock_profile.key = ""
        mock_config.get_profile.return_value = mock_profile
        mock_load_config.return_value = mock_config

        mock_runner = MagicMock()
        mock_runner.run.return_value = 0
        mock_runner_cls.return_value = mock_runner

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cmd_cp,
                ["/app", "/var/www/app", "-p", "staging"],
            )

            assert result.exit_code == 0, result.output

            scp_args = mock_runner.run.call_args[0][0]
            local_src = scp_args[-2]
            assert local_src == "/app"

    def test_relative_dest_resolved_to_absolute(self, mock_load_config, mock_runner_cls, _mock_pw):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.host = "staging.example.com"
        mock_profile.user = "deploy"
        mock_profile.port = 22
        mock_profile.key = ""
        mock_config.get_profile.return_value = mock_profile
        mock_load_config.return_value = mock_config

        mock_runner = MagicMock()
        mock_runner.run.return_value = 0
        mock_runner_cls.return_value = mock_runner

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cmd_cp,
                [":/var/www/app/config.php", "./config.php", "-p", "staging"],
            )

            assert result.exit_code == 0, result.output

            scp_args = mock_runner.run.call_args[0][0]
            local_dest = scp_args[-1]
            assert Path(local_dest).is_absolute(), (
                f"dest should be absolute, got: {local_dest}"
            )
            assert local_dest == str(Path("./config.php").resolve())

    def test_tilde_src_expanded(self, mock_load_config, mock_runner_cls, _mock_pw):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.host = "staging.example.com"
        mock_profile.user = "deploy"
        mock_profile.port = 22
        mock_profile.key = ""
        mock_config.get_profile.return_value = mock_profile
        mock_load_config.return_value = mock_config

        mock_runner = MagicMock()
        mock_runner.run.return_value = 0
        mock_runner_cls.return_value = mock_runner

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                cmd_cp,
                ["~/test.txt", "/var/www/test.txt", "-p", "staging"],
            )

            assert result.exit_code == 0, result.output

            scp_args = mock_runner.run.call_args[0][0]
            local_src = scp_args[-2]
            assert Path(local_src).is_absolute(), (
                f"src should be absolute after ~ expansion, got: {local_src}"
            )
            assert "~" not in local_src

    def test_dotdot_src_resolved(self, mock_load_config, mock_runner_cls, _mock_pw):
        mock_config = MagicMock()
        mock_profile = MagicMock()
        mock_profile.host = "staging.example.com"
        mock_profile.user = "deploy"
        mock_profile.port = 22
        mock_profile.key = ""
        mock_config.get_profile.return_value = mock_profile
        mock_load_config.return_value = mock_config

        mock_runner = MagicMock()
        mock_runner.run.return_value = 0
        mock_runner_cls.return_value = mock_runner

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("sub").mkdir()
            os.chdir("sub")

            result = runner.invoke(
                cmd_cp,
                ["../file.txt", "/var/www/file.txt", "-p", "staging"],
            )

            assert result.exit_code == 0, result.output

            scp_args = mock_runner.run.call_args[0][0]
            local_src = scp_args[-2]
            assert Path(local_src).is_absolute(), (
                f"src should be absolute, got: {local_src}"
            )
            assert ".." not in local_src
