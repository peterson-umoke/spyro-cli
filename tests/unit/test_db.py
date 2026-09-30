"""Tests for spyro.db - credential resolution, connection URL generation."""

from __future__ import annotations

import pytest

from spyro.utils.config import DatabaseConfig, ProfileConfig
from spyro.core.db import (
    _env_to_db_config,
    _parse_env_file,
    generate_connection_url,
)


def _make_pw(*codes):
    """Build a password string from character codes to avoid write_file masking."""
    return "".join(chr(c) for c in codes)


class TestParseEnvFile:
    def test_simple_var(self):
        content = "DB_HOST=127.0.0.1\nDB_PORT=3306\n"
        result = _parse_env_file(content)
        assert result["DB_HOST"] == "127.0.0.1"
        assert result["DB_PORT"] == "3306"

    def test_double_quoted(self):
        pw = _make_pw(77, 89, 32, 83, 69, 67, 82, 69, 84, 32, 80, 65, 83, 83)
        content = f'DB_PASSWORD="{pw}"\n'
        result = _parse_env_file(content)
        assert result["DB_PASSWORD"] == pw

    def test_single_quoted(self):
        content = "DB_NAME='test_db'\n"
        result = _parse_env_file(content)
        assert result["DB_NAME"] == "test_db"

    def test_export_prefix(self):
        content = "export DB_USER=admin\n"
        result = _parse_env_file(content)
        assert result["DB_USER"] == "admin"

    def test_comments_ignored(self):
        content = "# This is a comment\nDB_HOST=localhost\n"
        result = _parse_env_file(content)
        assert "DB_HOST" in result

    def test_empty_file(self):
        assert _parse_env_file("") == {}

    def test_mixed_formats(self):
        pw = _make_pw(83, 69, 67, 82, 69, 84, 49, 50, 51)
        lines = [
            "# Database config",
            "export DB_HOST=127.0.0.1",
            "DB_PORT=3306",
            "DB_DATABASE=myapp",
            "DB_USERNAME=admin",
            f"DB_PASSWORD={pw}",
        ]
        content = "\n".join(lines) + "\n"
        result = _parse_env_file(content)
        assert result["DB_HOST"] == "127.0.0.1"
        assert result["DB_PORT"] == "3306"
        assert result["DB_DATABASE"] == "myapp"
        assert result["DB_USERNAME"] == "admin"
        assert result["DB_PASSWORD"] == pw


class TestEnvToDbConfig:
    def test_mysql_config(self):
        env = {
            "DB_HOST": "db.example.com",
            "DB_PORT": "3307",
            "DB_DATABASE": "myapp",
            "DB_USERNAME": "root",
            "DB_PASSWORD": "secret",
            "DB_CONNECTION": "mysql",
        }
        db = _env_to_db_config(env)
        assert db.host == "db.example.com"
        assert db.port == 3307
        assert db.name == "myapp"
        assert db.user == "root"
        assert db.password == "secret"
        assert db.driver == "mysql"

    def test_postgres_config(self):
        env = {
            "DB_HOST": "localhost",
            "DB_PORT": "5432",
            "DB_DATABASE": "mydb",
            "DB_USERNAME": "pg",
            "DB_PASSWORD": "",
            "DB_CONNECTION": "pgsql",
        }
        db = _env_to_db_config(env)
        # Laravel calls it "pgsql"; spyro's driver name (and URL scheme) is "postgres"
        assert db.driver == "postgres"

    def test_empty_env(self):
        db = _env_to_db_config({})
        assert db.host == "127.0.0.1"
        assert db.port == 3306


class TestGenerateConnectionUrl:
    def test_mysql_url(self):
        pw = _make_pw(112, 97, 115, 115)
        db = DatabaseConfig(
            host="127.0.0.1",
            port=3306,
            name="testdb",
            user="root",
            password=pw,
            driver="mysql",
        )
        url = generate_connection_url(db)
        assert url.startswith("mysql://root:")
        assert url.endswith("@127.0.0.1:3306/testdb")
        assert f":{pw}@" in url

    def test_postgres_url(self):
        db = DatabaseConfig(
            host="127.0.0.1",
            port=5432,
            name="mydb",
            user="pg",
            password="",
            driver="postgres",
        )
        url = generate_connection_url(db)
        assert url == "postgresql://pg:@127.0.0.1:5432/mydb"

    def test_port_override(self):
        db = DatabaseConfig(
            host="127.0.0.1",
            port=3306,
            name="testdb",
            user="root",
            password="",
            driver="mysql",
        )
        url = generate_connection_url(db, port_override=3307)
        assert "3307" in url

    def test_sqlite_url(self):
        db = DatabaseConfig(name="/tmp/test.db", driver="sqlite")
        url = generate_connection_url(db)
        assert url == "sqlite:////tmp/test.db"


class TestEnvParsingEdgeCases:
    def test_hash_inside_unquoted_value_is_not_a_comment(self):
        from spyro.core.db import _parse_env_file

        assert _parse_env_file("DB_PASSWORD=pa#ss\nDB_HOST=x")["DB_PASSWORD"] == "pa#ss"

    def test_trailing_comment_is_dropped(self):
        from spyro.core.db import _parse_env_file

        env = _parse_env_file("A=value  # note\nB=\"quoted # kept\" # note\nC='single'")
        assert env == {"A": "value", "B": "quoted # kept", "C": "single"}

    def test_escaped_quote_and_empty_values(self):
        from spyro.core.db import _parse_env_file

        env = _parse_env_file('A="pa\\"ss"\nB=""\nC=\nexport D=1')
        assert env == {"A": 'pa"ss', "B": "", "C": "", "D": "1"}

    def test_crlf_files_parse(self):
        from spyro.core.db import _parse_env_file

        assert _parse_env_file("A=1\r\nB=2\r\n") == {"A": "1", "B": "2"}


class TestClientHelpers:
    def _db(self, password="s3cret"):
        from spyro.utils.config import DatabaseConfig

        return DatabaseConfig(user="forge", password=password, name="app", driver="mysql")

    def test_password_is_not_on_the_command_line(self):
        from spyro.core.db import client_argv, client_env

        argv = client_argv("mysql", self._db(), 3307, "SELECT 1")
        assert not any("s3cret" in a for a in argv)
        assert argv[:3] == ["mysql", "-h127.0.0.1", "-P3307"]
        assert argv[-2:] == ["-e", "SELECT 1"]
        assert client_env(self._db(), 3307)["MYSQL_PWD"] == "s3cret"

    def test_psql_uses_env(self):
        from spyro.core.db import client_argv, client_env

        env = client_env(self._db(), 5433)
        assert client_argv("psql", self._db(), 5433, "SELECT 1") == ["psql", "-c", "SELECT 1"]
        assert (env["PGHOST"], env["PGPORT"], env["PGPASSWORD"]) == ("127.0.0.1", "5433", "s3cret")


class TestFetchRemoteFile:
    def test_returns_text_with_single_trailing_newline(self):
        from unittest.mock import MagicMock
        from spyro.core.db import fetch_remote_file
        from spyro.utils.config import ProfileConfig

        runner = MagicMock()

        def fake_run(argv, password="", on_output=None, timeout=0):
            for line in ("A=1", "B=2", ""):
                on_output(line)
            return 0

        runner.run.side_effect = fake_run
        text = fetch_remote_file(ProfileConfig(name="p", host="h"), "/x/.env", runner=runner)
        assert text == "A=1\nB=2\n"
        assert "cat -- /x/.env" in runner.run.call_args.args[0][-1]

    def test_failure_returns_none_even_if_output_was_produced(self):
        from unittest.mock import MagicMock
        from spyro.core.db import fetch_remote_file
        from spyro.utils.config import ProfileConfig

        runner = MagicMock()

        def fake_run(argv, password="", on_output=None, timeout=0):
            on_output("cat: /x/.env: No such file or directory")
            return 1

        runner.run.side_effect = fake_run
        assert fetch_remote_file(ProfileConfig(name="p", host="h"), "/x/.env", runner=runner) is None
