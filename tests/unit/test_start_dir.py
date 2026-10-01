"""`spyro ssh` starts in remote_path and `spyro cp` resolves relative remote paths from it.

--home / --root switch both back to the account's home directory. Only profiles
that set remote_path in spyro.toml are affected (it defaults to /var/www).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main
from spyro.utils.config import ProfileConfig, parse_config

TOML = """\
[profiles.a]
host = "h.example.com"
user = "u"
remote_path = "/var/www/app"

[profiles.plain]
host = "p.example.com"
user = "u"

[profiles.tilde]
host = "t.example.com"
user = "u"
remote_path = "~/site"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    (proj / "spyro.toml").write_text(TOML)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SPYRO_PASSWORD", "pw")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
    return proj


def invoke(*args):
    return CliRunner().invoke(main, ["-q", *args])


def profile(remote_path="/var/www/app", set_=True) -> ProfileConfig:
    return ProfileConfig(name="a", host="h", remote_path=remote_path, remote_path_set=set_)


# ---------------------------------------------------------------------------
# config: remote_path_set
# ---------------------------------------------------------------------------


def test_remote_path_set_only_when_the_toml_sets_it(tmp_path):
    f = tmp_path / "spyro.toml"
    f.write_text(TOML + '\n[profiles.same_as_default]\nhost = "x"\nremote_path = "/var/www"\n')
    cfg = parse_config(f)
    assert cfg.profiles["a"].remote_path_set is True
    assert cfg.profiles["plain"].remote_path_set is False and cfg.profiles["plain"].remote_path == "/var/www"
    # explicitly writing the default value still counts as setting it
    assert cfg.profiles["same_as_default"].remote_path_set is True
    assert "remote_path_set" not in cfg.profiles["a"].extra


# ---------------------------------------------------------------------------
# _resolve_remote
# ---------------------------------------------------------------------------


class TestResolveRemote:
    @pytest.mark.parametrize(
        "given, expected",
        [
            (".env", "/var/www/app/.env"),
            (":.env", "/var/www/app/.env"),                 # the ':' remote marker
            ("storage/logs/", "/var/www/app/storage/logs/"),  # trailing slash = directory, kept
            ("./x", "/var/www/app/./x"),
            ("", "/var/www/app/"),                          # ":" alone = remote_path itself
            (":", "/var/www/app/"),
            (".", "/var/www/app/."),
            ("staging:sub/x", "/var/www/app/sub/x"),        # legacy "profile:path" form
        ],
    )
    def test_relative_paths_start_in_remote_path(self, given, expected):
        assert commands._resolve_remote(given, profile()) == expected

    @pytest.mark.parametrize(
        "given, expected",
        [
            ("/etc/x", "/etc/x"),
            (":/etc/x", "/etc/x"),              # only the ':' marker is removed
            ("staging:/etc/x", "/etc/x"),       # legacy "profile:path" form
            ("~/x", "~/x"),
            (":~/x", "~/x"),
            ("~", "~"),
            ("~other/x", "~other/x"),
        ],
    )
    def test_absolute_and_home_paths_are_left_alone(self, given, expected):
        assert commands._resolve_remote(given, profile()) == expected

    def test_from_home_leaves_relative_paths_relative(self):
        assert commands._resolve_remote(":.env", profile(), from_home=True) == ".env"
        assert commands._resolve_remote(":", profile(), from_home=True) == ""

    def test_profiles_without_remote_path_keep_the_old_home_relative_behaviour(self):
        assert commands._resolve_remote(":.env", profile(set_=False)) == ".env"

    def test_remote_path_trailing_slash_and_root(self):
        assert commands._resolve_remote("x", profile("/var/www/app/")) == "/var/www/app/x"
        assert commands._resolve_remote("x", profile("/")) == "/x"

    def test_tilde_remote_path_is_kept_for_scp_to_expand(self):
        assert commands._resolve_remote("x", profile("~/site")) == "~/site/x"


# ---------------------------------------------------------------------------
# the remote command `spyro ssh` runs, executed by real shells
# ---------------------------------------------------------------------------

LOGIN_SHELLS = [s for s in ("sh", "bash", "zsh", "dash", "csh", "tcsh", "ksh", "fish") if shutil.which(s)]


@pytest.fixture
def stub_shell(tmp_path):
    """A stand-in for the account's login shell: reports where it was started."""
    stub = tmp_path / "stub-login-shell"
    stub.write_text('#!/bin/sh\nprintf "PWD=%s ARGS=%s\\n" "$(pwd -P)" "$*"\n')
    stub.chmod(0o755)
    return stub


def run_remote(command: str, stub, home: Path, *, login_shell="sh", env_extra=None, cwd=None):
    """Run *command* the way sshd does: handed to the account's login shell as `-c <command>`."""
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "SHELL": str(stub), **(env_extra or {})}
    return subprocess.run(
        [shutil.which(login_shell), "-c", command],
        env=env, cwd=str(cwd or home), capture_output=True, text=True, timeout=20,
    )


class TestShellInDir:
    @pytest.mark.parametrize("login_shell", LOGIN_SHELLS)
    def test_lands_in_the_directory_as_a_login_shell_under_any_login_shell(self, login_shell, stub_shell, tmp_path):
        target = tmp_path / "deploy"
        target.mkdir()
        res = run_remote(commands._shell_in_dir(str(target)), stub_shell, tmp_path / "home", login_shell=login_shell)
        assert res.returncode == 0, res.stderr
        assert res.stdout.strip() == f"PWD={target.resolve()} ARGS=-l"

    @pytest.mark.parametrize("login_shell", LOGIN_SHELLS)
    def test_awkward_directory_names_are_quoted(self, login_shell, stub_shell, tmp_path):
        target = tmp_path / "it's a \"weird\" $HOME `id`; dir & more ünï"
        target.mkdir()
        res = run_remote(commands._shell_in_dir(str(target)), stub_shell, tmp_path / "home", login_shell=login_shell)
        assert res.stdout.strip() == f"PWD={target.resolve()} ARGS=-l", res.stderr

    def test_missing_directory_still_gives_a_shell_in_home_and_says_so(self, stub_shell, tmp_path):
        home = tmp_path / "home"
        res = run_remote(commands._shell_in_dir("/definitely/not/here"), stub_shell, home)
        assert res.returncode == 0
        assert res.stdout.strip() == f"PWD={home.resolve()} ARGS=-l"
        assert "/definitely/not/here not found on the server; starting in your home directory" in res.stderr

    def test_tilde_paths_expand_to_the_remote_home(self, stub_shell, tmp_path):
        home = tmp_path / "home"
        (home / "site").mkdir(parents=True)
        res = run_remote(commands._shell_in_dir("~/site"), stub_shell, home, cwd=tmp_path)
        assert res.stdout.strip() == f"PWD={(home / 'site').resolve()} ARGS=-l"

    def test_bare_tilde_is_home(self, stub_shell, tmp_path):
        home = tmp_path / "home"
        res = run_remote(commands._shell_in_dir("~"), stub_shell, home, cwd=tmp_path)
        assert res.stdout.strip() == f"PWD={home.resolve()} ARGS=-l"

    def test_falls_back_to_bin_sh_when_SHELL_is_unset(self, tmp_path):
        target = tmp_path / "deploy"
        target.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        env = {"PATH": os.environ["PATH"], "HOME": str(home)}
        res = subprocess.run(
            [shutil.which("sh"), "-c", commands._shell_in_dir(str(target))],
            env=env, cwd=str(home), input="pwd -P\nexit\n", capture_output=True, text=True, timeout=20,
        )
        assert str(target.resolve()) in res.stdout


class TestCapistranoCdWithTilde:
    def test_absolute_paths_are_unchanged(self):
        assert commands._capistrano_cd("/var/www/app") == (
            "cd /var/www/app && [ -L current ] && cd current || cd /var/www/app"
        )

    def test_tilde_remote_path_expands_and_enters_current(self, tmp_path):
        home = tmp_path / "home"
        (home / "app" / "releases" / "1").mkdir(parents=True)
        (home / "app" / "current").symlink_to(home / "app" / "releases" / "1")
        res = subprocess.run(
            ["sh", "-c", commands._capistrano_cd("~/app") + " && pwd -P"],
            env={"PATH": os.environ["PATH"], "HOME": str(home)}, capture_output=True, text=True,
        )
        assert res.stdout.strip() == str((home / "app" / "releases" / "1").resolve())


# ---------------------------------------------------------------------------
# spyro ssh / shell
# ---------------------------------------------------------------------------


class TestSshCommand:
    def _run(self, *args):
        runner_cls = MagicMock()
        runner_cls.return_value.interactive_run.return_value = 0
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke(*args)
        call = runner_cls.return_value.interactive_run.call_args
        return result, call

    def test_default_starts_in_remote_path(self, project):
        result, call = self._run("ssh", "-p", "a")
        argv = call.args[0]
        assert argv[1] == "-t"
        assert argv[-1].startswith("sh -c ") and "cd /var/www/app" in argv[-1]
        assert "in /var/www/app" in result.output
        assert call.kwargs["password"] == "pw"

    @pytest.mark.parametrize("flag", ["--home", "--root"])
    def test_home_flag_opens_a_plain_login_shell(self, project, flag):
        result, call = self._run("ssh", "-p", "a", flag)
        argv = call.args[0]
        assert argv[-1] == "u@h.example.com"          # no remote command: ssh's own login shell, in $HOME
        assert " in /" not in result.output

    def test_profile_without_remote_path_is_unchanged(self, project):
        _, call = self._run("ssh", "-p", "plain")
        assert call.args[0][-1] == "u@p.example.com"

    def test_shell_is_an_alias_with_the_same_flags(self, project):
        _, default_call = self._run("shell", "-p", "a")
        _, home_call = self._run("shell", "-p", "a", "--home")
        assert default_call.args[0][-1].startswith("sh -c ")
        assert home_call.args[0][-1] == "u@h.example.com"

    def test_tilde_remote_path_expands_on_the_server(self, project):
        _, call = self._run("ssh", "-p", "tilde")
        assert '"$HOME"/site' in call.args[0][-1]

    def test_no_sudo_password_is_injected_into_a_plain_shell(self, project):
        _, call = self._run("ssh", "-p", "a")
        assert "sudo_password" not in call.kwargs

    def test_end_to_end_the_generated_command_lands_in_remote_path(self, project, tmp_path, stub_shell):
        deploy = tmp_path / "srv" / "app"
        deploy.mkdir(parents=True)
        (project / "spyro.toml").write_text(f'[profiles.local]\nhost = "h"\nuser = "u"\nremote_path = "{deploy}"\n')
        _, call = self._run("ssh", "-p", "local")
        res = run_remote(call.args[0][-1], stub_shell, tmp_path / "home")
        assert res.stdout.strip() == f"PWD={deploy.resolve()} ARGS=-l"


# ---------------------------------------------------------------------------
# spyro cp
# ---------------------------------------------------------------------------


class TestCpCommand:
    def _run(self, project, *args):
        (project / ".env").write_text("A=1\n")
        runner_cls = MagicMock()
        runner_cls.return_value.run.return_value = 0
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke("cp", *args)
        assert result.exit_code == 0, result.output
        calls = [c.args[0] for c in runner_cls.return_value.run.call_args_list]
        return result, [c for c in calls if c[0] == "scp"], [c for c in calls if c[0] == "ssh"]

    def test_relative_remote_dest_starts_in_remote_path(self, project):
        result, scp, _ = self._run(project, ".env", ":.env", "-p", "a")
        assert scp[0][-1] == "u@h.example.com:/var/www/app/.env"
        assert "Copying .env -> /var/www/app/.env" in result.output      # the resolved path is shown

    def test_bare_colon_means_remote_path_itself(self, project):
        _, scp, _ = self._run(project, ".env", ":", "-p", "a")
        assert scp[0][-1] == "u@h.example.com:/var/www/app/"

    def test_dest_without_the_colon_marker_is_resolved_too(self, project):
        _, scp, _ = self._run(project, ".env", "sub/", "-p", "a")
        assert scp[0][-1] == "u@h.example.com:/var/www/app/sub/"

    def test_absolute_and_home_dests_are_unchanged(self, project):
        _, scp, _ = self._run(project, ".env", "/srv/other/.env", "-p", "a")
        assert scp[0][-1] == "u@h.example.com:/srv/other/.env"
        _, scp, _ = self._run(project, ".env", ":~/x", "-p", "a")
        assert scp[0][-1] == "u@h.example.com:~/x"

    @pytest.mark.parametrize("flag", ["--home", "--root"])
    def test_home_flag_copies_relative_to_the_home_directory(self, project, flag):
        _, scp, _ = self._run(project, ".env", ":.env", "-p", "a", flag)
        assert scp[0][-1] == "u@h.example.com:.env"

    def test_home_flag_does_not_touch_absolute_paths(self, project):
        _, scp, _ = self._run(project, ".env", "/srv/x/.env", "-p", "a", "--home")
        assert scp[0][-1] == "u@h.example.com:/srv/x/.env"

    def test_profile_without_remote_path_is_unchanged(self, project):
        _, scp, _ = self._run(project, ".env", ":.env", "-p", "plain")
        assert scp[0][-1] == "u@p.example.com:.env"

    def test_tilde_remote_path(self, project):
        _, scp, _ = self._run(project, ".env", ":.env", "-p", "tilde")
        assert scp[0][-1] == "u@t.example.com:~/site/.env"

    def test_download_source_is_resolved_against_remote_path(self, project):
        _, scp, _ = self._run(project, ":storage/logs/app.log", "./out.log", "-p", "a")
        assert scp[0][-2] == "u@h.example.com:/var/www/app/storage/logs/app.log"
        _, scp, _ = self._run(project, ":storage/logs/app.log", "./out.log", "-p", "a", "--home")
        assert scp[0][-2] == "u@h.example.com:storage/logs/app.log"

    def test_each_profile_uses_its_own_remote_path(self, project):
        _, scp, _ = self._run(project, ".env", ":.env", "--all")
        assert [c[-1] for c in scp] == [
            "u@h.example.com:/var/www/app/.env",
            "u@p.example.com:.env",
            "u@t.example.com:~/site/.env",
        ]

    def test_parents_creates_the_directory_under_remote_path(self, project):
        (project / "app").mkdir()
        (project / "app" / "Foo.php").write_text("x")
        _, scp, ssh = self._run(project, "app/Foo.php", ":", "--parents", "-p", "a")
        assert ssh[0][-1] == "mkdir -p /var/www/app/app"
        assert scp[0][-1] == "u@h.example.com:/var/www/app/app/Foo.php"

    def test_parents_with_a_tilde_remote_path_expands_on_the_server(self, project):
        (project / "app").mkdir()
        (project / "app" / "Foo.php").write_text("x")
        _, scp, ssh = self._run(project, "app/Foo.php", ":", "--parents", "-p", "tilde")
        assert ssh[0][-1] == 'mkdir -p "$HOME"/site/app'   # used to create a literal directory named "~"
        assert scp[0][-1] == "u@t.example.com:~/site/app/Foo.php"
