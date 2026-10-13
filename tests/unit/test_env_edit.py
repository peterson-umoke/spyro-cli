"""`spyro env` key-level editing: lossless parser, phpdotenv encoding, and the remote write path."""

from __future__ import annotations

import os
import random
import stat
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from spyro.utils import envedit

CORPUS = [
    "",
    "A=1",
    "A=1\nB=2\n",
    "A=1\r\nB='x y'\r\n",
    "# comment\n\nexport A=1 # trailing\n  B = 2\n",
    'KEY="-----BEGIN KEY-----\nabc\n-----END KEY-----"\nNEXT=1\n',
    "H='a#b$c'\nD=\"x \\\" ${APP_URL} # y\"\n",
    "UNTERMINATED=\"abc\nX=1\n",
    "garbage line\n",
]


@pytest.mark.parametrize("text", CORPUS)
def test_round_trip_is_byte_exact(text):
    assert envedit.serialize(envedit.parse(text)) == text


def test_parse_problems():
    assert envedit.problems(envedit.parse('A="x\nB=1\n')) == ["line 1: unterminated \" quote"]
    assert envedit.problems(envedit.parse("nope\n# ok\n\n")) == ["line 1: not KEY=VALUE"]


def test_encode_decode_fuzz():
    rng = random.Random(1)
    alphabet = "aZ09 _-./:@,+=%#$'\"\\\n\r\t{}!"
    for _ in range(3000):
        v = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        enc = envedit.encode(v)
        assert "\n" not in enc and "\r" not in enc
        assert envedit.decode(enc) == v, (v, enc)


def test_encode_forms():
    assert envedit.encode("abc-1.2") == "abc-1.2"
    assert envedit.encode("a b#$x") == "'a b#$x'"
    assert envedit.encode("it's $x") == '"it\'s \\$x"'


def test_set_value_keeps_export_spacing_comment_and_crlf():
    doc = envedit.parse("export A = old # note\r\nB=2\r\n")
    envedit.set_value(doc, 0, "new")
    assert envedit.serialize(doc) == "export A = new # note\r\nB=2\r\n"


def test_insert_after_hidden_copy_after_key_or_at_end():
    doc = envedit.parse("MAIL_HOST=x\n# MAIL_FROM=old\nDB_X=1\nLAST=1")
    envedit.insert(doc, "MAIL_FROM", "a")
    envedit.insert(doc, "MAIL_PORT", "b")
    envedit.insert(doc, "Z", "c", after="DB_X")
    assert envedit.serialize(doc) == "MAIL_HOST=x\n# MAIL_FROM=old\nMAIL_FROM=a\nDB_X=1\nZ=c\nLAST=1\nMAIL_PORT=b\n"


def test_comments_are_never_values():
    doc = envedit.parse("# A=old\nA=new\n## B=x\n# set C= to on\n#export D=1\n")
    assert envedit.find(doc, "A") == [1]
    assert [e.hidden for e in doc] == ["A", None, None, None, "D"]


def test_remove_hide_unhide_multiline():
    text = 'A=1\nK="x\ny"\nB=2\n'
    doc = envedit.parse(text)
    envedit.remove(doc, 1)
    assert envedit.serialize(doc) == "A=1\nB=2\n"
    doc = envedit.parse(text)
    envedit.hide(doc, 1)
    hidden = envedit.serialize(doc)
    assert hidden == 'A=1\n# K="x\n# y"\nB=2\n'
    doc = envedit.parse(hidden)
    assert envedit.find_hidden(doc, "K") == [1] and envedit.find(doc, "K") == []
    envedit.unhide(doc, 1)
    assert envedit.serialize(doc) == text


# ---------------------------------------------------------------------------
# End to end: fake ssh/scp run the remote scripts locally against a temp "server"
# ---------------------------------------------------------------------------

FAKE_SSH = f"""#!{sys.executable}
import os, sys
os.execvp("sh", ["sh", "-c", sys.argv[-1]])
"""
FAKE_SCP = f"""#!{sys.executable}
import os, shutil, sys
src, dest = sys.argv[-2], sys.argv[-1].split(":", 1)[1]
shutil.copy(src, os.path.join(os.environ["HOME"], dest))
"""
SECRET = "s3cr3t value$#'"


@pytest.fixture
def server(tmp_path, monkeypatch):
    bindir, home, app = tmp_path / "bin", tmp_path / "home", tmp_path / "app"
    for d in (bindir, home, app / "shared"):
        d.mkdir(parents=True)
    for name, body in (("ssh", FAKE_SSH), ("scp", FAKE_SCP)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    real = app / "shared" / ".env"
    real.write_text(f"APP_ENV=production\r\nMAIL_HOST=smtp\r\nDB_PASSWORD='{SECRET[:6]}'\r\n")
    real.chmod(0o640)
    (app / ".env").symlink_to(real)
    (tmp_path / "spyro.toml").write_text(
        f'[profiles.prod]\nhost = "h"\nuser = "u"\nremote_path = "{app}"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SPYRO_PASSWORD", "pw")
    return app, real, home


def run(*args, input=None):
    from spyro.cli.main import main

    return CliRunner().invoke(main, ["-q", "env", *args], input=input)


def test_keys_and_has(server):
    r = run("keys", "-p", "prod")
    assert r.exit_code == 0, r.output
    assert "MAIL_HOST" in r.output and "smtp" not in r.output and SECRET[:6] not in r.output
    assert run("has", "-p", "prod", "DB_PASSWORD").output.strip() == "DB_PASSWORD: set"
    assert run("has", "-p", "prod", "NOPE").exit_code == 1


def test_set_writes_atomically_keeps_symlink_mode_and_never_prints_value(server):
    app, real, home = server
    r = run("set", "-p", "prod", "DB_PASSWORD", "--stdin", "-y", "--json", input=SECRET + "\n")
    assert r.exit_code == 0, r.output
    assert SECRET not in r.output and "s3cr3t" not in r.output
    assert (app / ".env").is_symlink()
    text = real.read_bytes().decode()
    assert envedit.decode(envedit.raw_value(envedit.parse(text)[2])) == SECRET
    assert text.startswith("APP_ENV=production\r\nMAIL_HOST=smtp\r\n")  # untouched lines byte-exact
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    assert len(list(real.parent.glob(".env.spyro-bak.*"))) == 1
    assert not list(home.glob(".spyro-env-*"))  # staged upload removed

    r = run("set", "-p", "prod", "MAIL_PORT", "--stdin", "-y", input="587")
    assert r.exit_code == 0, r.output
    assert real.read_bytes().decode().endswith("\"\r\nMAIL_PORT=587\r\n")  # new key: end of file


def test_dry_run_and_noninteractive_without_yes_write_nothing(server):
    _, real, _ = server
    before = real.read_bytes()
    assert run("set", "-p", "prod", "X", "--generate", "16", "--dry-run").exit_code == 0
    assert run("set", "-p", "prod", "X", "--generate", "16").exit_code != 0
    assert real.read_bytes() == before


def test_hide_unhide_and_missing_keys(server):
    _, real, _ = server
    assert run("unset", "-p", "prod", "NOPE", "-y").exit_code != 0
    assert run("hide", "-p", "prod", "NOPE", "-y").exit_code != 0
    assert run("hide", "-p", "prod", "MAIL_HOST", "-y").exit_code == 0
    assert "# MAIL_HOST=smtp\r\n" in real.read_bytes().decode()
    assert run("has", "-p", "prod", "MAIL_HOST").exit_code == 1
    assert run("hide", "-p", "prod", "MAIL_HOST", "-y").exit_code == 0  # already hidden: no-op
    assert run("set", "-p", "prod", "MAIL_HOST", "--stdin", "-y", input="new").exit_code == 0
    assert "# MAIL_HOST=smtp\r\nMAIL_HOST=new\r\n" in real.read_bytes().decode()
    r = run("unhide", "-p", "prod", "MAIL_HOST", "-y")
    assert r.exit_code != 0 and "already set" in r.output
    assert run("unset", "-p", "prod", "MAIL_HOST", "-y").exit_code == 0
    assert run("unhide", "-p", "prod", "MAIL_HOST", "-y").exit_code == 0
    assert "\r\nMAIL_HOST=smtp\r\n" in real.read_bytes().decode()


def test_duplicates_unset_restore(server):
    _, real, _ = server
    real.write_text(real.read_bytes().decode() + "MAIL_HOST=dup\r\n")
    r = run("set", "-p", "prod", "MAIL_HOST", "--stdin", "-y", input="x")
    assert r.exit_code != 0 and "--all" in r.output
    assert run("unset", "-p", "prod", "MAIL_HOST", "--all", "-y").exit_code == 0
    assert "MAIL_HOST" not in real.read_bytes().decode()
    assert run("restore", "-p", "prod", "-y").exit_code == 0
    assert real.read_bytes().decode().count("MAIL_HOST") == 2


def test_concurrent_edit_is_refused(server, monkeypatch):
    """The file changes between fetch and write: compare-and-swap refuses, file untouched."""
    from spyro.cli import commands

    _, real, _ = server
    orig = commands._env_read

    def racing_read(*a, **kw):
        out = orig(*a, **kw)
        real.write_text("SOMEONE=else\n")
        return out

    monkeypatch.setattr(commands, "_env_read", racing_read)
    r = run("set", "-p", "prod", "X", "--stdin", "-y", input="1")
    assert r.exit_code != 0 and "changed since it was read" in r.output
    assert real.read_bytes().decode() == "SOMEONE=else\n"


def test_laravel_profile_reloads_after_write(server, tmp_path):
    """Cached config is rebuilt and workers restarted, or the app never sees the edit."""
    app, real, _ = server
    toml = tmp_path / "spyro.toml"
    toml.write_text(toml.read_text() + "artisan = true\n")
    (app / "bootstrap" / "cache").mkdir(parents=True)
    (app / "bootstrap" / "cache" / "config.php").write_text("<?php return [];")
    (app / "vendor" / "laravel" / "horizon").mkdir(parents=True)
    calls = tmp_path / "php.log"
    php = tmp_path / "bin" / "php"
    php.write_text(f'#!/bin/sh\necho "$PWD $*" >>{calls}\n')
    php.chmod(0o755)

    assert run("set", "-p", "prod", "X", "--stdin", "-y", "--no-reload", input="1").exit_code == 0
    assert not calls.exists()
    r = run("set", "-p", "prod", "X", "--stdin", "-y", input="2")
    assert r.exit_code == 0, r.output
    assert calls.read_text().splitlines() == [
        f"{app} artisan config:cache", f"{app} artisan horizon:terminate", f"{app} artisan queue:restart",
    ]
