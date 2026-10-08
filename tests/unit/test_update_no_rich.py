"""`spyro update` must not use rich after the install swaps rich's files."""
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from spyro.cli.main import main


def test_update_prints_result_without_rich_after_install():
    ok = MagicMock(returncode=0, stderr="")
    with patch("spyro.cli.commands._fetch_latest_version", return_value="99.0.0"), \
         patch("spyro.cli.commands.shutil.which", return_value="/bin/uv"), \
         patch("spyro.cli.commands.subprocess.run", return_value=ok), \
         patch("spyro.cli.commands.console") as con:
        # rich is "broken" once the install has run
        con.print.side_effect = [None] * 5 + [RuntimeError("rich broken")]
        r = CliRunner().invoke(main, ["-q", "update"])
    assert r.exit_code == 0, r.output
    assert "spyro updated to v99.0.0" in r.output
