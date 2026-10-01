"""Regression tests for staging smoke-test findings.

Remote failures must become spyro's exit code, progress chatter must stay off
stdout (``eval --json`` is machine output), ``env diff`` must never print
values, and ``logs laravel`` must follow Laravel's daily log files.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main


TOML = """\
[profiles.sud]
host = "s.example.com"
user = "u"
remote_path = "{root}/sud"
artisan = true
sudo = true

[profiles.plain]
host = "p.example.com"
user = "u"
remote_path = "{root}/plain"
artisan = true

[profiles.noart]
host = "n.example.com"
user = "u"
remote_path = "{root}/noart"
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    (proj / "spyro.toml").write_text(TOML.format(root=tmp_path / "srv"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SPYRO_PASSWORD", "pw")
    monkeypatch.delenv("SPYRO_PROFILE", raising=False)
    monkeypatch.chdir(proj)
    monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
    return proj


def invoke(*args):
    return CliRunner().invoke(main, list(args))


def fake_runner(*codes, output=""):
    """PTYRunner whose successive run() calls return *codes* (last one repeats)."""
    cls = MagicMock()
    seq = list(codes)

    def run(_args, **kw):
        if output and kw.get("on_output"):
            kw["on_output"](output)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    cls.return_value.run.side_effect = run
    return cls


@pytest.fixture
def script_file(tmp_path):
    f = tmp_path / "s.php"
    f.write_text("<?php echo 1;")
    return f


# ---------------------------------------------------------------------------
# the remote exit code is spyro's exit code
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_run_exits_with_the_remote_code(self, project):
        with patch.object(commands, "PTYRunner", fake_runner(7)):
            assert invoke("run", "-p", "plain", "exit 7").exit_code == 7

    def test_run_all_visits_every_profile_then_reports_the_first_failure(self, project):
        cls = fake_runner(0, 5, 9)
        with patch.object(commands, "PTYRunner", cls):
            result = invoke("run", "--all", "true")
        assert cls.return_value.run.call_count == 3
        assert result.exit_code == 5

    def test_run_without_a_profile_is_a_usage_error(self, project):
        assert invoke("run", "true").exit_code != 0

    def test_service_command_without_sudo_rights_fails(self, project):
        cls = fake_runner(0)
        with patch.object(commands, "PTYRunner", cls):
            result = invoke("supervisor", "status", "-p", "plain")
        assert result.exit_code != 0
        cls.return_value.run.assert_not_called()

    def test_php_version_fallback_still_succeeds(self, project):
        with patch.object(commands, "PTYRunner", fake_runner(1, 0)):
            assert invoke("php", "version", "-p", "plain").exit_code == 0

    def test_script_exits_with_the_php_exit_code_and_still_cleans_up(self, project, script_file):
        cls = fake_runner(0, 255, 0)  # scp ok, php fatal, rm ok
        with patch.object(commands, "PTYRunner", cls):
            result = invoke("script", str(script_file), "-p", "plain")
        assert result.exit_code == 255
        assert cls.return_value.run.call_args_list[-1].args[0][-1].startswith("rm -f ")

    def test_failed_upload_fails_eval_and_runs_nothing(self, project):
        cls = fake_runner(1)
        with patch.object(commands, "PTYRunner", cls):
            result = invoke("eval", "1+1", "-p", "plain")
        assert result.exit_code == 1
        assert cls.return_value.run.call_count == 1

    def test_tinker_file_exits_with_the_remote_code(self, project, script_file):
        with patch.object(commands, "PTYRunner", fake_runner(0, 3, 0)):
            assert invoke("tinker", "-p", "plain", "-f", str(script_file)).exit_code == 3

    @pytest.mark.parametrize("args", [
        ("eval", "1", "-p", "noart"),
        ("artisan", "--version", "-p", "noart"),
        ("tinker", "-p", "noart", "-e", "1"),
    ])
    def test_profile_without_artisan_is_an_error(self, project, args):
        cls = fake_runner(0)
        with patch.object(commands, "PTYRunner", cls):
            assert invoke(*args).exit_code != 0
        cls.return_value.run.assert_not_called()

    def test_upload_progress_stays_off_stdout_for_file_commands(self, project, script_file):
        Path(".env").write_text("A=1\n")
        for args in (
            ("script", str(script_file), "-p", "plain"),
            ("tinker", "-p", "plain", "-f", str(script_file)),
            ("env", "push", "-p", "plain"),
        ):
            with patch.object(commands, "PTYRunner", fake_runner(0)):
                result = invoke(*args)
            assert result.exit_code == 0, (args, result.output)
            assert "Uploading" not in result.stdout and "Uploading" in result.stderr, args

    def test_env_push_of_a_missing_file_fails(self, project):
        assert invoke("env", "push", "-p", "plain", "missing.env").exit_code != 0


# ---------------------------------------------------------------------------
# stdout is for results only
# ---------------------------------------------------------------------------


def test_eval_json_stdout_is_only_the_json(project):
    with patch.object(commands, "PTYRunner", fake_runner(0, output='"12.63.0"')):
        result = invoke("eval", "app()->version()", "-p", "plain", "--json")
    assert json.loads(result.stdout) == "12.63.0"
    assert "Uploading" in result.stderr


# ---------------------------------------------------------------------------
# env diff never prints values
# ---------------------------------------------------------------------------

REMOTE = (
    "# comment stays\n"
    "APP_KEY=base64:REMOTESECRET\n"
    'DB_PASSWORD="p@ss word"\n'
    "SAME=shared-value\n"
    'PRIVATE_KEY="-----BEGIN\nMULTILINEBODY\n-----END"\n'
    "ONLYREMOTE=remote-only-val\n"
)
LOCAL = (
    "APP_KEY=base64:LOCALSECRET\n"
    "SAME=shared-value\n"
    "ONLYLOCAL=local-only-val\n"
)
VALUES = ("REMOTESECRET", "LOCALSECRET", "p@ss word", "shared-value", "MULTILINEBODY",
          "remote-only-val", "local-only-val")


class TestEnvDiff:
    def _run(self, local: str | None):
        if local is not None:
            Path(".env").write_text(local)
        with patch.object(commands, "fetch_remote_file", return_value=REMOTE):
            return invoke("env", "diff", "-p", "plain")

    def test_no_value_is_printed(self, project):
        out = self._run(LOCAL).output
        assert not [v for v in VALUES if v in out]

    def test_changed_added_and_removed_keys_are_still_visible(self, project):
        out = self._run(LOCAL).output
        assert "APP_KEY" in out and "ONLYREMOTE" in out and "ONLYLOCAL" in out
        assert "DB_PASSWORD" in out

    def test_identical_values_are_not_reported_as_a_change(self, project):
        out = self._run(LOCAL).output
        assert not [l for l in out.splitlines() if l[:1] in "+-" and "SAME" in l]

    def test_identical_files_report_no_difference(self, project):
        assert "identical" in self._run(REMOTE).output

    def test_without_a_local_env_only_keys_are_shown(self, project):
        out = self._run(None).output
        assert not [v for v in VALUES if v in out]
        assert "APP_KEY" in out


# ---------------------------------------------------------------------------
# logs laravel follows daily files
# ---------------------------------------------------------------------------


class TestLogsLaravel:
    def _tail(self, project, tmp_path, files: dict[str, str]) -> str:
        logs = tmp_path / "srv" / "plain" / "storage" / "logs"
        logs.mkdir(parents=True)
        for i, (name, body) in enumerate(files.items()):
            f = logs / name
            f.write_text(body)
            os.utime(f, (1_700_000_000 + i * 86400,) * 2)
        with patch.object(commands, "_run_svc_cmd") as svc:
            invoke("logs", "laravel", "-p", "plain", "-n", "5")
        return subprocess.run(
            ["sh", "-c", svc.call_args.args[1]], capture_output=True, text=True
        ).stdout.strip()

    def test_picks_the_newest_daily_file_over_an_empty_laravel_log(self, project, tmp_path):
        out = self._tail(project, tmp_path, {
            "laravel.log": "", "laravel-2026-09-30.log": "old\n", "laravel-2026-10-01.log": "new\n",
        })
        assert out == "new"

    def test_single_file_layout_still_works(self, project, tmp_path):
        assert self._tail(project, tmp_path, {"laravel.log": "only\n"}) == "only"

    def test_missing_logs_say_so(self, project, tmp_path):
        assert self._tail(project, tmp_path, {}) == "Log not found"


# ---------------------------------------------------------------------------
# nginx status needs root to read certificates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile, expected", [("sud", "sudo nginx -t"), ("plain", "nginx -t")])
def test_nginx_status_escalates_only_on_sudo_profiles(project, profile, expected):
    with patch.object(commands, "_run_svc_cmd") as svc:
        invoke("nginx", "status", "-p", profile)
    assert svc.call_args.args[1].startswith(expected)


# ---------------------------------------------------------------------------
# redaction edge cases (synthetic values only)
# ---------------------------------------------------------------------------


def test_escaped_quotes_do_not_end_a_multiline_value_early():
    from spyro.utils.envdiff import redact_env

    text = 'CREDS="{\\"a\\": \\"SECRET1\\",\n\\"b\\": \\"SECRET2\\",\n\\"c\\": \\"SECRET3\\"}"\nAFTER=visible\n'
    out = "\n".join(redact_env(text))
    assert "SECRET" not in out
    assert out == "CREDS=***\nAFTER=***"


def test_unusual_key_names_are_still_redacted():
    from spyro.utils.envdiff import redact_env

    out = redact_env("MY-KEY=DASHSECRET\nexport EXP=EXPSECRET\n#OLD=COMMENTSECRET\n# plain comment\n")
    assert "SECRET" not in "\n".join(out)
    assert out[-1] == "# plain comment"


def test_an_unterminated_quote_fails_closed():
    from spyro.utils.envdiff import redact_env

    out = redact_env('K="never closed\nBODY1\nBODY2\n')
    assert out == ["K=***"]
