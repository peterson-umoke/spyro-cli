"""`spyro update` must not use rich after the install swaps rich's files."""
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from spyro.cli.main import main


def test_update_prints_result_without_rich_after_install():
    ok = MagicMock(returncode=0, stderr="")
    with patch("spyro.cli.commands._fetch_latest_version", return_value="99.0.0"), \
         patch("spyro.cli.commands._fetch_release_notes", return_value=""), \
         patch("spyro.cli.commands.shutil.which", return_value="/bin/uv"), \
         patch("spyro.cli.commands.subprocess.run", return_value=ok), \
         patch("spyro.cli.commands.console") as con:
        # rich is "broken" once the install has run
        con.print.side_effect = [None] * 5 + [RuntimeError("rich broken")]
        r = CliRunner().invoke(main, ["-q", "update"])
    assert r.exit_code == 0, r.output
    assert "spyro updated to v99.0.0" in r.output


def test_update_check_shows_release_notes_verbatim():
    """Release bodies may contain [brackets]; they must print as plain text, not rich markup."""
    with patch("spyro.cli.commands._fetch_latest_version", return_value="99.0.0"), \
         patch("spyro.cli.commands._fetch_release_notes", return_value="- fix [/var/www] crash"):
        r = CliRunner().invoke(main, ["-q", "update", "--check"])
    assert r.exit_code == 0, r.output
    assert "What's new" in r.output and "- fix [/var/www] crash" in r.output


def test_latest_version_comes_from_releases_only_never_tags():
    import urllib.error

    from spyro.cli import commands

    urls = []

    def fake_urlopen(req, timeout=0):
        urls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 404, "no releases", {}, None)

    with patch("urllib.request.urlopen", fake_urlopen):
        assert commands._fetch_latest_version() is None
    assert urls == ["https://api.github.com/repos/peterson-umoke/spyro-cli/releases/latest"]
