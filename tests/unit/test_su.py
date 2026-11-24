"""`su = true` profiles: no root SSH login and no sudo; root is reached with
`su` and a root password stored apart from the SSH password."""

from __future__ import annotations

import os
import stat
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main
from spyro.core.pty_engine import SU_MARKER, PTYRunner, su_command
from spyro.utils.config import parse_config
from spyro.utils.keychain import SU_ACCOUNT, get_credential

TOML = """\
[profiles.box]
host = "h.example.com"
user = "u"
remote_path = "/var/www/app"
artisan = true
su = true
"""

# Stands in for the real su: asks like util-linux su does, then runs the command.
FAKE_SU = """#!/bin/sh
stty -echo; printf 'Password: '; read -r pw; stty echo; echo
[ "$pw" = root-pw ] || { echo "su: Authentication failure"; exit 1; }
[ "$1" = root ] && [ "$2" = -c ] || exit 64
exec sh -c "$3"
"""


@pytest.fixture
def fake_su_env(tmp_path):
    su = tmp_path / "bin" / "su"
    su.parent.mkdir()
    su.write_text(FAKE_SU)
    su.chmod(su.stat().st_mode | stat.S_IXUSR)
    return {**os.environ, "PATH": f"{su.parent}:{os.environ['PATH']}"}


def _run(cmd: str, env, **kw) -> tuple[int, list[str]]:
    out: list[str] = []
    ec = PTYRunner().run(["sh", "-c", cmd], on_output=out.append, timeout=10, env=env, **kw)
    return ec, [line.strip() for line in out if line.strip()]


class TestEngine:
    def test_su_prompt_gets_the_root_password_and_the_command_runs(self, fake_su_env):
        # No SSH password prompt at all (key auth): the bare "Password:" must
        # still go to su, never be mistaken for sshd's.
        ec, out = _run(
            su_command("sudo echo as-root && sudo -u nobody true; echo done"),
            fake_su_env, password="ssh-pw", sudo_password="root-pw", su=True,
        )
        assert "as-root" in out and "done" in out, out
        assert ec == 0
        assert SU_MARKER not in " ".join(out)
        assert not any("ssh-pw" in line or "root-pw" in line for line in out)

    def test_root_shell_can_find_sbin_tools(self, fake_su_env):
        # Found on a real Debian server: su keeps the SSH user's PATH (no sbin),
        # so `sudo -u www-data ...` died with "runuser: command not found".
        _, out = _run(su_command('echo "$PATH"'), {**fake_su_env, "PATH": fake_su_env["PATH"].split(":")[0] + ":/usr/bin:/bin"},
                      sudo_password="root-pw", su=True)
        assert any(line.startswith("/usr/local/sbin:/usr/sbin:/sbin:") for line in out), out

    def test_exit_status_of_the_command_is_kept(self, fake_su_env):
        ec, _ = _run(su_command("exit 7"), fake_su_env, sudo_password="root-pw", su=True)
        assert ec == 7

    def test_wrong_root_password_fails_instead_of_hanging(self, fake_su_env):
        ec, out = _run(su_command("echo never"), fake_su_env, sudo_password="nope", su=True)
        assert ec == 1 and "never" not in out
        assert any("Authentication failure" in line for line in out)

    def test_missing_root_password_aborts_with_a_hint(self, fake_su_env):
        ec, out = _run(su_command("echo never"), fake_su_env, password="ssh-pw", su=True)
        assert ec == 1
        assert any("auth set" in line and "--su" in line for line in out), out

    def test_shim_passes_env_assignments_and_arguments_through(self, fake_su_env):
        _, out = _run(
            su_command("sudo X=1 sh -c 'echo \"$X $1\"' sh 'a b'"),
            fake_su_env, sudo_password="root-pw", su=True,
        )
        assert "1 a b" in out, out


class TestConfigAndCredentials:
    def test_su_implies_escalation_is_available(self, tmp_path):
        path = tmp_path / "spyro.toml"
        path.write_text(TOML)
        p = parse_config(path).get_profile("box")
        assert p.su and p.sudo and "su" not in p.extra

    def test_root_password_never_falls_back_to_the_ssh_password(self, monkeypatch):
        monkeypatch.setenv("SPYRO_PASSWORD", "ssh-pw")
        monkeypatch.setattr("spyro.utils.keychain._keyring_available", lambda: False)
        assert get_credential("box", "u") == "ssh-pw"
        assert get_credential("box", SU_ACCOUNT) is None
        monkeypatch.setenv("SPYRO_SU_PASSWORD_BOX", "root-pw")
        assert get_credential("box", SU_ACCOUNT) == "root-pw"
        assert get_credential("box", "u") == "ssh-pw"


class TestCli:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        home, proj = tmp_path / "home", tmp_path / "proj"
        home.mkdir()
        proj.mkdir()
        (proj / "spyro.toml").write_text(TOML)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SPYRO_PASSWORD", "ssh-pw")
        monkeypatch.setenv("SPYRO_SU_PASSWORD", "root-pw")
        monkeypatch.chdir(proj)
        monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
        return proj

    def _invoke(self, *args):
        runner_cls = MagicMock()
        runner_cls.return_value.run.return_value = 0
        with patch.object(commands, "PTYRunner", runner_cls):
            result = CliRunner().invoke(main, ["-q", *args])
        assert result.exit_code == 0, result.output
        return runner_cls.return_value.run

    def test_service_command_runs_in_a_root_shell_with_the_root_password(self, project):
        run = self._invoke("nginx", "restart", "-p", "box")
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        assert argv[1] == "-t"
        assert argv[-1].startswith(f"echo {SU_MARKER}; su root -c ")
        assert "systemctl restart nginx" in argv[-1]
        assert kwargs["password"] == "ssh-pw"
        assert kwargs["sudo_password"] == "root-pw" and kwargs["su"] is True

    def test_run_escalates_only_when_the_command_says_sudo(self, project):
        run = self._invoke("run", "uptime", "-p", "box")
        assert run.call_args.args[0][-1] == "uptime"
        assert run.call_args.kwargs["sudo_password"] == "" and "su" not in run.call_args.kwargs

        run = self._invoke("run", "sudo systemctl restart nginx", "-p", "box")
        assert run.call_args.args[0][-1] == su_command("sudo systemctl restart nginx")
        assert run.call_args.kwargs["su"] is True

    def test_no_escalate_stays_the_ssh_user(self, project):
        run = self._invoke("artisan", "migrate", "--no-escalate", "-p", "box")
        assert "su root" not in run.call_args.args[0][-1]
        assert run.call_args.kwargs["sudo_password"] == ""
