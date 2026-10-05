from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from spyro.cli.commands import cmd_cp, cmd_run


def _profile(*, sudo: bool = True) -> MagicMock:
    return MagicMock(
        host="staging.example.com",
        user="deploy",
        port=22,
        key="",
        sudo=sudo,
        remote_path="/srv/app",
    )


def test_cp_escalates_copy_to_nonwritable_remote_destination(tmp_path: Path) -> None:
    source = tmp_path / "config.php"
    source.write_text("content")
    config = MagicMock()
    config.get_profile.return_value = _profile()
    runner = MagicMock()
    runner.run.side_effect = [42, 0, 0, 0]

    with (
        patch("spyro.cli.commands.load_config", return_value=config),
        patch("spyro.cli.commands.PTYRunner", return_value=runner),
        patch("spyro.cli.commands.time.sleep") as delay,
        patch("spyro.utils.keychain.prompt_for_credential", return_value="secret"),
    ):
        result = CliRunner().invoke(cmd_cp, [str(source), ":/etc/app.conf", "-p", "staging"])

    assert result.exit_code == 0, result.output
    assert "sudo" in result.output.lower()
    delay.assert_called_once_with(3)
    commands = [call.args[0] for call in runner.run.call_args_list]
    assert any(args[0] == "ssh" and "-w" in args[-1] for args in commands)
    assert any(args[0] == "ssh" and "sudo cp -R" in args[-1] for args in commands)
    assert any(args[0] == "scp" and "/etc/app.conf" not in args[-1] for args in commands)


def test_cp_writable_destination_uses_direct_scp(tmp_path: Path) -> None:
    source = tmp_path / "config.php"
    source.write_text("content")
    config = MagicMock()
    config.get_profile.return_value = _profile()
    runner = MagicMock()
    runner.run.side_effect = [0, 0]

    with (
        patch("spyro.cli.commands.load_config", return_value=config),
        patch("spyro.cli.commands.PTYRunner", return_value=runner),
        patch("spyro.cli.commands.time.sleep") as delay,
        patch("spyro.utils.keychain.prompt_for_credential", return_value="secret"),
    ):
        result = CliRunner().invoke(cmd_cp, [str(source), ":/srv/app/config.php", "-p", "staging"])

    assert result.exit_code == 0, result.output
    assert runner.run.call_args_list[-1].args[0][0] == "scp"
    assert all("sudo" not in call.args[0][-1] for call in runner.run.call_args_list)
    delay.assert_not_called()


def test_cp_cancel_does_not_stage_or_write(tmp_path: Path) -> None:
    source = tmp_path / "config.php"
    source.write_text("content")
    config = MagicMock()
    config.get_profile.return_value = _profile()
    runner = MagicMock()
    runner.run.return_value = 42

    with (
        patch("spyro.cli.commands.load_config", return_value=config),
        patch("spyro.cli.commands.PTYRunner", return_value=runner),
        patch("spyro.cli.commands.time.sleep", side_effect=KeyboardInterrupt),
        patch("spyro.utils.keychain.prompt_for_credential", return_value="secret"),
    ):
        result = CliRunner().invoke(cmd_cp, [str(source), ":/etc/app.conf", "-p", "staging"])

    assert "cancelled" in result.output.lower()
    assert len(runner.run.call_args_list) == 1
    assert runner.run.call_args.args[0][0] == "ssh"


def test_cp_remote_to_local_does_not_run_remote_write_preflight(tmp_path: Path) -> None:
    config = MagicMock()
    config.get_profile.return_value = _profile()
    runner = MagicMock()
    runner.run.return_value = 0
    target = tmp_path / "copy.out"

    with (
        patch("spyro.cli.commands.load_config", return_value=config),
        patch("spyro.cli.commands.PTYRunner", return_value=runner),
        patch("spyro.utils.keychain.prompt_for_credential", return_value="secret"),
    ):
        result = CliRunner().invoke(cmd_cp, [":/etc/app.conf", str(target), "-p", "staging"])

    assert result.exit_code == 0, result.output
    assert len(runner.run.call_args_list) == 1
    assert runner.run.call_args.args[0][0] == "scp"


def test_run_enables_sudo_prompt_only_for_explicit_sudo_command() -> None:
    config = MagicMock()
    config.get_profile.return_value = _profile()

    with (
        patch("spyro.cli.commands.load_config", return_value=config),
        patch("spyro.cli.commands._run_svc_cmd", return_value=0) as run_remote,
    ):
        runner = CliRunner()
        plain = runner.invoke(cmd_run, ["-p", "staging", "echo sudo"])
        explicit = runner.invoke(cmd_run, ["-p", "staging", "id && sudo whoami"])

    assert plain.exit_code == 0, plain.output
    assert explicit.exit_code == 0, explicit.output
    assert run_remote.call_args_list[0].kwargs["escalate"] is False
    assert run_remote.call_args_list[1].kwargs["escalate"] is True
