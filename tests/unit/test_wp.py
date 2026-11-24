"""`spyro wp` behaves like `spyro artisan`: flags pass through, -p is optional
with one profile, --timeout works, and WP-CLI is found without a second login."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main

TOML = """\
[profiles.site]
host = "h.example.com"
user = "u"
remote_path = "/var/www/html"
wordpress = true
{extra}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    def make(extra: str = ""):
        home, proj = tmp_path / "home", tmp_path / "proj"
        home.mkdir()
        proj.mkdir()
        (proj / "spyro.toml").write_text(TOML.format(extra=extra))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("SPYRO_PASSWORD", "pw")
        monkeypatch.chdir(proj)
        monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
    return make


def wp(*args):
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    # Any plain ssh here would be a second, password-less login: forbid it.
    with patch.object(commands, "PTYRunner", runner_cls), \
            patch.object(subprocess, "run", side_effect=AssertionError("plain ssh used")):
        result = CliRunner().invoke(main, ["-q", "wp", *args])
    return result, runner_cls.return_value.run


def test_wp_flags_are_passed_through_and_profile_is_optional(project):
    project()
    result, run = wp("plugin", "list", "--status=active", "--format=json")
    assert result.exit_code == 0, result.output
    remote = run.call_args.args[0][-1]
    assert remote.endswith('"$@" plugin list --status=active --format=json')
    assert "cd /var/www/html" in remote
    assert run.call_count == 1


def test_wp_timeout_and_arguments_with_spaces(project):
    project()
    result, run = wp("--timeout", "300", "option", "update", "blogname", "My Site", "-p", "site")
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["timeout"] == 300.0
    assert run.call_args.args[0][-1].endswith("\"$@\" option update blogname 'My Site'")


def test_wp_lookup_finds_the_binary_and_keeps_its_exit_status(tmp_path):
    fake = tmp_path / "wp"
    fake.write_text("#!/bin/sh\necho \"ran $*\"\nexit 3\n")
    fake.chmod(0o755)
    (tmp_path / "php").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / "php").chmod(0o755)
    done = subprocess.run(
        ["sh", "-c", f'{commands._wp_lookup(False)} && "$@" a b'],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}, capture_output=True, text=True,
    )
    assert (done.stdout.strip(), done.returncode) == ("ran a b", 3)


def test_wp_cli_path_and_sudo_user_are_used(project):
    project('sudo = true\nsudo_user = "www-data"\nwp_cli_path = "/opt/wp cli/wp"')
    result, run = wp("cache", "flush", "-p", "site")
    assert result.exit_code == 0, result.output
    assert run.call_args.args[0][-1].endswith("&& sudo -u www-data '/opt/wp cli/wp' cache flush")
    assert run.call_args.args[0][1] == "-t"



def test_wp_as_root_is_refused_before_connecting_with_the_fix_spelled_out(project):
    # Found on a real WordPress server: WP-CLI answers a root run with a
    # screenful ("YIKES!") whose suggested fix does not apply to spyro.
    project("sudo = true")
    result, run = wp("core", "version", "-p", "site")
    assert result.exit_code != 0 and run.call_count == 0
    assert "sudo_user" in result.output and "--allow-root" in result.output

    result, run = wp("core", "version", "--allow-root", "-p", "site")
    assert result.exit_code == 0 and run.call_args.args[0][-1].endswith('sudo "$@" core version --allow-root')

    result, run = wp("core", "version", "--no-escalate", "-p", "site")
    assert result.exit_code == 0 and "sudo" not in run.call_args.args[0][-1]


def _lookup_env(tmp_path, downloader: str | None):
    """A fake server with no network: only the tools the installer needs, a `php`
    that accepts any phar, and a `curl` or `wget` that "downloads" a wp script."""
    import shutil

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("sh", "mktemp", "cp", "chmod", "mkdir", "rm"):
        (bin_ / tool).symlink_to(shutil.which(tool))
    if downloader:
        (bin_ / "php").write_text("#!/bin/sh\nexit 0\n")
        (bin_ / downloader).write_text(
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do case $1 in -o|-qO) dest=$2;; esac; shift; done\n'
            'printf \'#!/bin/sh\\necho "wp $*"\\n\' > "$dest"\n'
        )
    for f in bin_.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    return {"PATH": str(bin_), "HOME": str(tmp_path)}


@pytest.mark.parametrize("downloader", ["curl", "wget"])
def test_wp_is_installed_when_missing_and_found_there_next_time(tmp_path, downloader):
    env = _lookup_env(tmp_path, downloader)
    script = f'{commands._wp_lookup(False)} && "$@" core version'
    first = subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "installing it" in first.stdout and first.stdout.strip().endswith("wp core version")
    assert (tmp_path / ".local/bin/wp").exists()

    second = subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True)
    assert second.stdout.strip() == "wp core version"  # no reinstall, no notice


def test_wp_install_failure_stops_before_running_anything(tmp_path):
    env = _lookup_env(tmp_path, None)  # php, but no curl/wget
    php = tmp_path / "bin" / "php"
    php.write_text("#!/bin/sh\nexit 0\n")
    php.chmod(0o755)
    done = subprocess.run(
        ["/bin/sh", "-c", f'{commands._wp_lookup(False)} && echo ran'],
        env=env, capture_output=True, text=True,
    )
    assert done.returncode == 127 and "ran" not in done.stdout
    assert "wp_cli_path" in done.stdout


def test_wp_without_php_says_so(tmp_path):
    env = _lookup_env(tmp_path, None)
    done = subprocess.run(["/bin/sh", "-c", f'{commands._wp_lookup(False)} && echo ran'], env=env, capture_output=True, text=True)
    assert done.returncode == 127 and "PHP was not found" in done.stdout and "ran" not in done.stdout


def test_wp_for_another_user_ignores_a_copy_in_the_login_users_home(tmp_path):
    # sudo_user cannot read the SSH user's home, so that copy must not be chosen.
    env = _lookup_env(tmp_path, "curl")
    home_wp = tmp_path / ".local/bin/wp"
    home_wp.parent.mkdir(parents=True)
    home_wp.write_text("#!/bin/sh\necho home-copy\n")
    home_wp.chmod(0o755)
    (tmp_path / "bin/sudo").write_text('#!/bin/sh\necho "sudo $*"\n')
    (tmp_path / "bin/sudo").chmod(0o755)
    script = commands._wp_lookup(True) + ' ; echo "chosen=$wp"'
    out = subprocess.run(["/bin/sh", "-c", script], env=env, capture_output=True, text=True).stdout
    assert "home-copy" not in out and str(home_wp) not in out.split("chosen=")[-1]
    assert "/usr/local/bin/wp" in out  # found there, or installed there
