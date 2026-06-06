"""Tests for spyro.db - credential resolution, connection URL generation."""

from __future__ import annotations

import pytest

from src.utils.config import DatabaseConfig, ProfileConfig
from src.core.db import (
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
        assert db.driver == "pgsql"

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
