"""Regressions for what running every command against real servers turned up
(Laravel/MariaDB/nginx and WordPress containers): wrong exit codes, commands
that could not use a stored password, noisy output, and option inconsistencies."""

from __future__ import annotations

import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main
from spyro.core.pty_engine import PTYRunner
from spyro.core.sync import SyncPin
from spyro.utils.keychain import SU_ACCOUNT

TOML = """\
[profiles.a]
host = "h.example.com"
user = "u"
remote_path = "/var/www/app"
artisan = true
sudo = true
sudo_user = "www-data"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    (proj / "spyro.toml").write_text(TOML)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SPYRO_PASSWORD", "pw")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
    return proj


def invoke(*args):
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    with patch.object(commands, "PTYRunner", runner_cls):
        result = CliRunner().invoke(main, ["-q", *args])
    return result, runner_cls.return_value.run


def test_cp_without_a_profile_is_a_failure_not_exit_0(project, monkeypatch):
    monkeypatch.delenv("SPYRO_PROFILE", raising=False)
    (project / "f").write_text("x")
    result, run = invoke("cp", "f", "/tmp/f")
    assert result.exit_code != 0 and run.call_count == 0


def test_profile_is_optional_everywhere_when_there_is_only_one(project):
    result, run = invoke("nginx", "version")
    assert result.exit_code == 0, result.output
    # server daemons live in sbin, which a non-login SSH PATH lacks
    assert run.call_args.args[0][-1] == 'PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"; nginx -v 2>&1 || echo \'Nginx not found\''


def test_timeout_is_accepted_by_eval_script_and_tinker(project):
    (project / "s.php").write_text("<?php echo 1;")
    for args in (("eval", "1+1", "--timeout", "300"), ("script", "s.php", "--timeout", "300"),
                 ("tinker", "-e", "1", "--timeout", "300")):
        result, run = invoke(*args)
        assert result.exit_code == 0, (args, result.output)
        assert 300.0 in [c.kwargs.get("timeout") for c in run.call_args_list], args


def test_tinker_file_with_a_php_tag_is_required_not_piped(project):
    (project / "t.php").write_text("<?php echo 1;")
    (project / "bare.php").write_text("echo 1;")
    _, run = invoke("tinker", "-f", "t.php")
    assert any("tinker --execute='require \"/tmp/spyro-tinker-" in c.args[0][-1] for c in run.call_args_list)
    _, run = invoke("tinker", "-f", "bare.php")
    assert any("php artisan tinker < /tmp/spyro-tinker-" in c.args[0][-1] for c in run.call_args_list)


def test_old_names_still_work_but_are_not_listed(project):
    for group, old, new in (("logs", "supervisor", "tunnel"), ("env", "remove", "unset")):
        listing = CliRunner().invoke(main, [group, "--help"]).output.split("Commands:")[-1]
        assert f"  {new} " in listing and f"  {old} " not in listing
        assert CliRunner().invoke(main, [group, old, "--help"]).exit_code == 0


def test_ps_logs_in_with_the_stored_password_before_its_plain_ssh(project, monkeypatch):
    done = MagicMock(returncode=0, stdout="root 1 init\n", stderr="")
    with patch.object(commands.subprocess, "run", return_value=done):
        _, run = invoke("ps")
        assert run.call_count == 1 and run.call_args.kwargs["password"] == "pw"
        assert run.call_args.args[0][-1] == "true"

        monkeypatch.delenv("SPYRO_PASSWORD")  # key or agent auth: no login, and never a prompt
        with patch("spyro.utils.keychain._keyring_available", return_value=False):
            _, run = invoke("ps")
        assert run.call_count == 0


def test_auth_set_su_stores_the_root_password_under_its_own_entry(project):
    with patch("spyro.utils.keychain.store_credential", return_value=True) as store, \
            patch("spyro.utils.keychain.get_credential", return_value=None):
        result = CliRunner().invoke(main, ["-q", "auth", "set", "-p", "a", "--su", "-w", "root-pw"])
    assert result.exit_code == 0, result.output
    store.assert_called_once_with("a", SU_ACCOUNT, "root-pw")


def test_sync_initial_and_delete(project, capsys):
    site = project / "site"
    (site / "node_modules" / "x").mkdir(parents=True)
    (site / "inc").mkdir()
    for rel in ("a.css", "inc/b.php", ".env", "node_modules/x/i.js"):
        (site / rel).write_text("x")
    pin = SyncPin(local_path=str(site), remote_path="/srv/site", profile="a")
    stop = threading.Event()
    stop.set()  # initial pass only, then leave the watch loop at once
    commands._run_sync_watch("a", [pin], dry_run=True, stop=stop, initial=True)
    out = capsys.readouterr().out
    assert "Would sync: a.css" in out and "Would sync: inc/b.php" in out
    assert "i.js" not in out and "Would sync: .env" not in out  # secrets and pruned directories


def test_engine_drops_the_echoed_enter_and_ssh_closing_line():
    child = (
        "import sys,termios;a=termios.tcgetattr(0);a[3]&=~termios.ECHO;termios.tcsetattr(0,termios.TCSANOW,a);"  # like sudo: no echo
        "sys.stdout.write('[sudo] password for u: ');sys.stdout.flush();sys.stdin.readline();"
        "print();print('real output');print();print('kept blank above');print('Shared connection to h.example.com closed.')"
    )
    out: list[str] = []
    assert PTYRunner().run([sys.executable, "-c", child], sudo_password="s", on_output=out.append, timeout=10) == 0
    lines = [line.rstrip() for line in out]
    assert lines[0] == "real output" and "" in lines  # only the echo right after the password is dropped
    assert not any("closed." in line for line in lines)
