"""CLI-level regression tests: each one pins a bug found in the code review."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main


TOML = """\
[profiles.a]
host = "h.example.com"
user = "u"
remote_path = "/var/www/app"
artisan = true
sudo = true
sudo_user = "www-data"
forwarded_ports = [3306]

[profiles.a.db]
host = "127.0.0.1"
port = 3306
name = "appdb"
user = "forge"
password = "s3cret"
driver = "mysql"

[profiles.plain]
host = "p.example.com"
user = "u"
remote_path = "/srv/app"
artisan = true
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with spyro.toml, an isolated HOME and a known password."""
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


def invoke(*args, **kw):
    return CliRunner().invoke(main, ["-q", *args], **kw)


# ---------------------------------------------------------------------------
# Remote output is data, never Rich markup
# ---------------------------------------------------------------------------


def test_remote_lines_are_printed_verbatim(capsys):
    for line in ("[stacktrace] at Foo.php:10", "failed at [/var/www/app/x.php]", ":smile: [red]x[/red]"):
        commands._remote(line)
    out = capsys.readouterr().out.splitlines()
    assert out == ["[stacktrace] at Foo.php:10", "failed at [/var/www/app/x.php]", ":smile: [red]x[/red]"]


def test_remote_long_lines_are_not_wrapped(capsys):
    commands._remote("x" * 300)
    assert capsys.readouterr().out.strip() == "x" * 300


def test_json_output_is_never_wrapped(capsys):
    commands._emit_json([{"command": "php-fpm: pool www " + "y" * 200}])
    assert json.loads(capsys.readouterr().out)[0]["command"].endswith("y" * 200)


# ---------------------------------------------------------------------------
# ps
# ---------------------------------------------------------------------------


class TestPs:
    def _run(self, project, *extra, stdout="", returncode=0):
        with patch.object(commands.subprocess, "run") as run:
            run.return_value = MagicMock(stdout=stdout, stderr="", returncode=returncode)
            result = invoke("ps", "-p", "a", *extra)
        return result, run.call_args.args[0][-1]

    def test_grep_filters_the_whole_command_not_just_a_fallback(self, project):
        _, remote = self._run(project, "--grep", "php", stdout="x")
        assert remote == "ps aux | grep -i -- php"

    def test_json_keeps_the_full_command_and_is_valid(self, project):
        long_cmd = "php-fpm: pool www " + "z" * 150
        row = f"www 123 0.1 0.2 100 50 ? S {long_cmd}\n"
        result, remote = self._run(project, "--json", stdout=row)
        data = json.loads(result.output)
        assert data == [{"user": "www", "pid": "123", "cpu": "0.1", "mem": "0.2", "vsz": "100",
                         "rss": "50", "tty": "?", "stat": "S", "command": long_cmd}]
        assert "args=" in remote  # header-less, fixed columns

    def test_no_match_is_not_an_error(self, project):
        result, _ = self._run(project, "--grep", "nothing", stdout="", returncode=1)
        assert "No matching processes" in result.output


# ---------------------------------------------------------------------------
# cp --parents
# ---------------------------------------------------------------------------


def test_cp_parents_with_colon_prefixed_dest_does_not_create_a_dir_named_colon(project):
    (project / "app").mkdir()
    (project / "app" / "Foo.php").write_text("x")
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    with patch.object(commands, "PTYRunner", runner_cls):
        result = invoke("cp", "app/Foo.php", ":/var/www/app", "--parents", "-p", "a")
    assert result.exit_code == 0, result.output
    mkdir_cmd = runner_cls.return_value.run.call_args_list[0].args[0][-1]
    assert mkdir_cmd == "mkdir -p /var/www/app/app"
    scp_dest = runner_cls.return_value.run.call_args_list[1].args[0][-1]
    assert scp_dest == "u@h.example.com:/var/www/app/app/Foo.php"


def test_cp_failure_exits_nonzero_and_timeout_is_configurable(project):
    (project / "f").write_text("x")
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 124
    with patch.object(commands, "PTYRunner", runner_cls):
        result = invoke("cp", "f", "/tmp/f", "-p", "a", "--timeout", "500")
    assert result.exit_code == 1
    assert "timed out after 500s" in result.output
    assert runner_cls.return_value.run.call_args.kwargs["timeout"] == 500.0


# ---------------------------------------------------------------------------
# app commands: sudo_user, --no-escalate, timeouts
# ---------------------------------------------------------------------------


class TestAppCommands:
    def _run(self, *args):
        runner_cls = MagicMock()
        runner_cls.return_value.run.return_value = 0
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke(*args)
        return result, runner_cls.return_value.run

    def test_sudo_user_is_used_for_app_commands(self, project):
        result, run = self._run("artisan", "migrate", "-p", "a")
        argv = run.call_args.args[0]
        assert argv[1] == "-t"  # remote tty so sudo can prompt
        assert argv[-1] == "cd /var/www/app && [ -L current ] && cd current || cd /var/www/app && sudo -u www-data php artisan migrate"
        assert run.call_args.kwargs["sudo_password"] == "pw"

    def test_no_escalate_runs_as_login_user_without_tty(self, project):
        _, run = self._run("artisan", "migrate", "--no-escalate", "-p", "a")
        argv = run.call_args.args[0]
        assert "-t" not in argv and "sudo" not in argv[-1]
        assert run.call_args.kwargs["sudo_password"] == ""

    def test_tinker_eval_uses_the_pty_with_the_ssh_password(self, project):
        _, run = self._run("tinker", "-p", "a", "-e", "User::count()")
        kwargs = run.call_args.kwargs
        assert kwargs["password"] == "pw" and kwargs["sudo_password"] == "pw"
        assert "--execute='User::count()'" in run.call_args.args[0][-1]

    def test_tinker_eval_no_escalate_still_sends_the_ssh_password(self, project):
        _, run = self._run("tinker", "-p", "a", "-e", "1", "--no-escalate")
        assert run.call_args.kwargs["password"] == "pw"
        assert run.call_args.kwargs["sudo_password"] == ""

    def test_timeout_message_explains_the_remote_may_still_run(self, project):
        runner_cls = MagicMock()
        runner_cls.return_value.run.return_value = 124
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke("artisan", "migrate", "-p", "plain", "--timeout", "7")
        assert "Timed out after 7s" in result.output and "--timeout" in result.output

    def test_log_follow_has_no_timeout(self, project):
        _, run = self._run("logs", "laravel", "-p", "plain", "-f")
        assert run.call_args.kwargs["timeout"] is None

    def test_log_path_is_quoted_and_exit_code_printed_once(self, project, monkeypatch):
        runner_cls = MagicMock()
        runner_cls.return_value.run.return_value = 3
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke("logs", "laravel", "-p", "plain")
        assert result.output.count("Exit code: 3") == 1

    def test_supervisor_process_name_is_quoted(self, project):
        _, run = self._run("supervisor", "restart", "x; touch /tmp/pwned", "-p", "a")
        assert run.call_args.args[0][-1] == "sudo supervisorctl restart 'x; touch /tmp/pwned'"

    def test_upload_and_run_uses_unguessable_name_and_always_cleans_up(self, project, tmp_path):
        script = tmp_path / "s.php"
        script.write_text("<?php echo 1;")
        runner_cls = MagicMock()
        runner_cls.return_value.run.side_effect = [0, 1, 0]  # scp ok, php fails, rm ok
        with patch.object(commands, "PTYRunner", runner_cls):
            result = invoke("script", str(script), "-p", "plain")
        calls = [c.args[0] for c in runner_cls.return_value.run.call_args_list]
        remote = calls[0][-1].split(":", 1)[1]
        assert len(remote.rsplit("-", 1)[1]) == len("0" * 32 + ".php")
        assert calls[2][-1] == f"rm -f {remote}"         # cleaned up although php failed
        assert remote in calls[1][-1]
        assert "Exit code: 1" in result.output


# ---------------------------------------------------------------------------
# tunnels: failures are reported
# ---------------------------------------------------------------------------


def test_up_reports_failure_and_exits_nonzero(project):
    with patch.object(commands, "TunnelManager") as mgr:
        mgr.return_value.start.side_effect = RuntimeError("Host key verification failed.")
        result = invoke("up", "a")
    assert result.exit_code == 1
    assert "Failed to start 'a': Host key verification failed." in result.output


def test_up_without_args_skips_profiles_that_forward_nothing(project):
    with patch.object(commands, "TunnelManager") as mgr:
        mgr.return_value.start.return_value = MagicMock(pid=1, forwarded_ports=[3306])
        invoke("up")
    assert [c.args[0] for c in mgr.return_value.start.call_args_list] == ["a"]


# ---------------------------------------------------------------------------
# credentials never echoed
# ---------------------------------------------------------------------------


def test_auth_list_reveals_nothing_about_the_password(project, monkeypatch):
    monkeypatch.setenv("SPYRO_PASSWORD", "hunter2-password")
    result = invoke("auth", "list")
    assert "hunter2" not in result.output and "pa" not in result.output.split("characters")[0].split("stored,")[-1]
    assert "password stored" in result.output


def test_pull_env_sends_password_and_writes_owner_only(project):
    with patch.object(commands, "fetch_remote_file", return_value="A=1\n") as fetch:
        result = invoke("env", "pull", "-p", "a", "--dest", "out.env")
    assert result.exit_code == 0, result.output
    assert fetch.call_args.kwargs["password"] == "pw"
    assert Path("out.env").read_text() == "A=1\n"
    assert stat.S_IMODE(os.stat("out.env").st_mode) == 0o600


def test_env_diff_of_identical_files_reports_identical(project):
    Path(".env").write_text("A=1\nB=2\n")
    with patch.object(commands, "fetch_remote_file", return_value="A=1\nB=2\n"):
        result = invoke("env", "diff", "-p", "a")
    assert "identical" in result.output


# ---------------------------------------------------------------------------
# db: password handling and tunnel selection
# ---------------------------------------------------------------------------


class TestDb:
    @pytest.fixture
    def fake_client(self, tmp_path, monkeypatch):
        """A `mysql` on PATH that records its argv and MYSQL_PWD."""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        rec = tmp_path / "rec.json"
        (bindir / "mysql").write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            f"open({str(rec)!r},'w').write(json.dumps({{'argv': sys.argv, 'pwd': os.environ.get('MYSQL_PWD')}}))\n"
            "print('id')\nprint('1')\n"
        )
        (bindir / "mysql").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(commands, "_detect_db_client", lambda profile: "mysql")  # a real mariadb may be installed
        return rec

    def test_query_passes_password_in_env_and_uses_the_db_forward(self, project, fake_client):
        from spyro.supervisor.state import TunnelState

        state = TunnelState(profile="a", local_port=6379, forwarded_ports=[3310], status="running")
        with patch.object(commands, "_tunnel_for", return_value=state):
            result = invoke("db", "query", "SELECT 1", "-p", "a")
        assert result.exit_code == 0, result.output
        rec = json.loads(fake_client.read_text())
        assert rec["pwd"] == "s3cret"
        assert not any("s3cret" in a for a in rec["argv"])
        assert "-P3310" in rec["argv"]
        assert result.output.split() == ["id", "1"]

    def test_bare_db_shows_help_instead_of_crashing(self, project):
        result = invoke("db")
        assert result.exit_code == 0 and "Commands:" in result.output

    def test_empty_password_triggers_remote_env_detection(self, project, fake_client):
        from spyro.supervisor.state import TunnelState
        from spyro.utils.config import DatabaseConfig

        (project / "spyro.toml").write_text(TOML.replace('password = "s3cret"', 'password = ""'))
        detected = DatabaseConfig(name="fromenv", user="envuser", password="envpw")
        state = TunnelState(profile="a", local_port=6379, forwarded_ports=[3310], status="running")
        with patch.object(commands, "resolve_db_credentials", return_value=detected) as resolve, \
                patch.object(commands, "_tunnel_for", return_value=state):
            result = invoke("db", "query", "SELECT 1", "-p", "a")
        assert resolve.called and result.exit_code == 0, result.output
        assert json.loads(fake_client.read_text())["pwd"] == "envpw"


# ---------------------------------------------------------------------------
# db dump: streamed bytes, safe quoting, private atomic file
# ---------------------------------------------------------------------------

FAKE_DUMP_SSH = r'''
import json, os, sys
args = sys.argv[1:]
if args[-1] == "true":                       # PTY auth step (opens the "master")
    sys.stdout.write("u@h's password: "); sys.stdout.flush(); sys.stdin.readline(); sys.exit(0)
pw = sys.stdin.readline().rstrip("\n")        # plain streaming step: password arrives on stdin
open(os.environ["REC"], "w").write(json.dumps({"args": args, "stdin_pw": pw}))
if os.environ.get("FAKE_FAIL"):
    sys.stderr.write("mysqldump: Got error: 1045: Access denied\n"); sys.exit(2)
sys.stderr.write("warning on stderr must not reach the dump\n")
sys.stdout.buffer.write(b"-- dump\r\nINSERT 'a\x00b\x1b[31m\x07';\nline\r\n")
'''


class TestDump:
    @pytest.fixture
    def fake(self, project, tmp_path, monkeypatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "ssh").write_text(f"#!{sys.executable}\n{FAKE_DUMP_SSH}")
        (bindir / "ssh").chmod(0o755)
        rec = tmp_path / "rec.json"
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("REC", str(rec))
        monkeypatch.delenv("FAKE_FAIL", raising=False)
        return rec

    def test_bytes_arrive_untouched_and_stderr_stays_out(self, fake):
        result = invoke("db", "dump", "-p", "a", "-o", "out.sql")
        assert result.exit_code == 0, result.output
        assert Path("out.sql").read_bytes() == b"-- dump\r\nINSERT 'a\x00b\x1b[31m\x07';\nline\r\n"
        assert stat.S_IMODE(os.stat("out.sql").st_mode) == 0o600
        assert not Path("out.sql.part").exists()

    def test_password_goes_through_stdin_not_the_command_line(self, fake):
        invoke("db", "dump", "-p", "a", "-o", "out.sql")
        rec = json.loads(fake.read_text())
        assert rec["stdin_pw"] == "s3cret"
        assert "s3cret" not in " ".join(rec["args"])
        assert "BatchMode=yes" in rec["args"]
        assert rec["args"][-1].startswith("sh -c ") and "read -r MYSQL_PWD" in rec["args"][-1]

    def test_where_and_table_names_are_shell_quoted(self, fake):
        invoke("db", "dump", "-p", "a", "-o", "out.sql", "-t", "users;touch /tmp/pwned", "-w", "id > 100")
        import shlex

        outer = json.loads(fake.read_text())["args"][-1]
        assert outer.startswith("sh -c ")
        cmd = shlex.split(outer)[2]                       # what sh will actually parse
        argv = shlex.split(cmd.split("&& ")[-1])
        assert argv[argv.index("--where") + 1] == "id > 100"
        assert "users;touch /tmp/pwned" in argv           # one literal argument, not a command
        assert "-P3306" in argv

    def test_gzip_output_is_a_valid_gzip_of_the_raw_stream(self, fake):
        import gzip

        result = invoke("db", "dump", "-p", "a", "-o", "out", "-z")
        assert result.exit_code == 0, result.output
        assert gzip.open("out.gz", "rb").read().startswith(b"-- dump\r\n")

    def test_failure_leaves_no_file_and_reports_mysqldump_error(self, fake, monkeypatch):
        monkeypatch.setenv("FAKE_FAIL", "1")
        result = invoke("db", "dump", "-p", "a", "-o", "out.sql")
        assert result.exit_code == 1
        assert "Access denied" in result.output
        assert not Path("out.sql").exists() and not Path("out.sql.part").exists()


# ---------------------------------------------------------------------------
# update notice: stderr, after the command, never in the data stream
# ---------------------------------------------------------------------------


def test_json_output_is_clean_even_when_an_update_is_available(project, monkeypatch):
    home = Path(os.environ["HOME"]) / ".spyro"
    home.mkdir(parents=True, exist_ok=True)
    (home / "version_check").write_text(json.dumps(
        {"checked_at": time.time(), "needs_update": True, "latest_version": "99.0.0"}))
    monkeypatch.setattr(commands, "_CHECK_CACHE", None)
    result = CliRunner().invoke(main, ["status", "--json"])  # not -q: update check enabled
    assert json.loads(result.stdout) == {"tunnels": [], "status": "no_tunnels"}


def test_update_notice_goes_to_stderr_when_interactive(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(commands, "_CHECK_CACHE", (True, "99.0.0"))
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    commands.notify_update()
    captured = capsys.readouterr()
    assert "Update available" in captured.err and captured.out == ""


def test_failed_update_check_is_cached_so_offline_runs_stay_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(commands, "_CHECK_CACHE", None)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    calls = []
    monkeypatch.setattr(commands, "_fetch_latest_version", lambda timeout=15: calls.append(timeout))
    commands.notify_update()
    monkeypatch.setattr(commands, "_CHECK_CACHE", None)  # new process
    commands.notify_update()
    assert calls == [3]                                   # short timeout, and only once


def test_bare_invocation_prints_help():
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0 and "Usage:" in result.output


# ---------------------------------------------------------------------------
# sync / watch
# ---------------------------------------------------------------------------


def test_watch_uploads_as_the_profile_user_creates_dirs_and_skips_secrets(project, tmp_path):
    pytest.importorskip("watchdog")
    src = tmp_path / "site"
    src.mkdir()
    pin = commands.SyncPin(local_path=str(src), remote_path="/var/www/site", profile="a", framework="laravel")

    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    stop = threading.Event()
    with patch.object(commands, "PTYRunner", runner_cls):
        t = threading.Thread(target=commands._run_sync_watch, args=("a", [pin]), kwargs={"stop": stop})
        t.start()
        time.sleep(1.0)                                  # let the observer start
        (src / "sub").mkdir()
        (src / "sub" / "page.php").write_text("<?php")
        (src / ".env").write_text("SECRET=1")
        (src / "storage" / "framework" / "sessions").mkdir(parents=True)
        (src / "storage" / "framework" / "sessions" / "abc").write_text("s")
        deadline = time.time() + 10
        while time.time() < deadline and not any(
            "page.php" in c.args[0][-1] for c in runner_cls.return_value.run.call_args_list
        ):
            time.sleep(0.2)
        time.sleep(1.0)
        stop.set()
        t.join(10)

    cmds = [c.args[0] for c in runner_cls.return_value.run.call_args_list]
    assert any(c[-1] == "mkdir -p /var/www/site/sub" for c in cmds)
    scp = [c for c in cmds if c[0] == "scp"]
    assert [c[-1] for c in scp] == ["u@h.example.com:/var/www/site/sub/page.php"]   # user@ present; no .env, no session


# ---------------------------------------------------------------------------
# review follow-ups
# ---------------------------------------------------------------------------


def test_sudo_user_can_read_the_uploaded_file(project, tmp_path):
    script = tmp_path / "s.php"
    script.write_text("<?php")
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    with patch.object(commands, "PTYRunner", runner_cls):
        invoke("script", str(script), "-p", "a")
    cmds = [c.args[0][-1] for c in runner_cls.return_value.run.call_args_list]
    chmod = [c for c in cmds if c.startswith("chmod 644 /tmp/spyro-script-")]
    assert len(chmod) == 1
    assert cmds.index(chmod[0]) < next(i for i, c in enumerate(cmds) if "sudo -u www-data php" in c)


def test_no_chmod_when_running_as_root_or_login_user(project, tmp_path):
    script = tmp_path / "s.php"
    script.write_text("<?php")
    runner_cls = MagicMock()
    runner_cls.return_value.run.return_value = 0
    with patch.object(commands, "PTYRunner", runner_cls):
        invoke("script", str(script), "-p", "plain")
    assert not any(c.args[0][-1].startswith("chmod") for c in runner_cls.return_value.run.call_args_list)


def test_watch_uploads_the_final_write_not_the_first(project, tmp_path):
    """Format-on-save: a second write 100ms after the first must not be dropped."""
    pytest.importorskip("watchdog")
    src = tmp_path / "site"
    src.mkdir()
    pin = commands.SyncPin(local_path=str(src), remote_path="/r", profile="a", framework="laravel")
    uploaded: list[str] = []

    def fake_run(argv, **kw):
        if argv[0] == "scp":
            uploaded.append(Path(argv[-2]).read_text())   # what is on disk at upload time
        return 0

    runner_cls = MagicMock()
    runner_cls.return_value.run.side_effect = fake_run
    stop = threading.Event()
    with patch.object(commands, "PTYRunner", runner_cls):
        t = threading.Thread(target=commands._run_sync_watch, args=("a", [pin]), kwargs={"stop": stop})
        t.start()
        time.sleep(1.0)
        f = src / "a.php"
        f.write_text("first")
        time.sleep(0.1)
        f.write_text("first-second")
        deadline = time.time() + 10
        while time.time() < deadline and "first-second" not in uploaded:
            time.sleep(0.1)
        stop.set()
        t.join(10)
    assert uploaded and uploaded[-1] == "first-second"
    assert len(uploaded) == 1                              # coalesced into one upload
