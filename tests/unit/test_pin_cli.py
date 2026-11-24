"""`spyro pin` / `unpin` must fail loudly: each of these used to print a
message (or even "Pinned"/"Removed") and exit 0."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from spyro.cli import commands
from spyro.cli.main import main
from spyro.core.sync import load_pins


@pytest.fixture
def project(tmp_path, monkeypatch):
    home, proj = tmp_path / "home", tmp_path / "proj"
    home.mkdir()
    (proj / "site").mkdir(parents=True)
    (proj / "spyro.toml").write_text('[profiles.a]\nhost = "h.example.com"\nuser = "u"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(proj)
    monkeypatch.setattr(commands, "_CHECK_CACHE", (False, ""))
    return proj


def run(*args):
    return CliRunner().invoke(main, ["-q", *args])


def test_pin_rejects_a_missing_directory_a_file_and_an_unknown_profile(project):
    (project / "file.txt").write_text("x")
    for args in (("pin", "nope", "/srv", "-p", "a"), ("pin", "file.txt", "/srv", "-p", "a"),
                 ("pin", "site", "/srv", "-p", "ghost")):
        result = run(*args)
        assert result.exit_code != 0, (args, result.output)
    assert load_pins() == []


def test_unpin_of_something_not_pinned_is_an_error_and_keeps_the_others(project):
    assert run("pin", "site", "/srv/site", "-p", "a").exit_code == 0
    assert run("unpin", "elsewhere", "-p", "a").exit_code != 0
    assert len(load_pins()) == 1
    result = run("unpin", "site", "-p", "a")  # relative path, same directory
    assert result.exit_code == 0 and load_pins() == []
