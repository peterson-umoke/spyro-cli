"""Tests for spyro.config — TOML parsing, validation, template generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.config import (
    DatabaseConfig,
    ProfileConfig,
    SpyroConfig,
    generate_config,
    parse_config,
)


SAMPLE_TOML = """\
[profiles.staging]
host = "staging.example.com"
user = "deploy"
port = 22
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [33060, 63790]

[profiles.staging.db]
host = "127.0.0.1"
port = 33060
name = "app_staging"
user = "forge"
password = "secret123"
driver = "mysql"

[profiles.production]
host = "production.example.com"
user = "root"
port = 2222
remote_path = "/opt/app"

[profiles.production.db]
host = "127.0.0.1"
port = 5432
name = "app_prod"
user = "postgres"
password = ""
driver = "postgres"
"""


class TestParseConfig:
    def test_parses_all_profiles(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        assert len(config.profiles) == 2
        assert "staging" in config.profiles
        assert "production" in config.profiles

    def test_profile_fields(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        p = config.profiles["staging"]
        assert p.host == "staging.example.com"
        assert p.user == "deploy"
        assert p.port == 22
        assert p.remote_path == "/var/www/app"
        assert p.artisan is True
        assert p.sudo is True
        assert p.forwarded_ports == [33060, 63790]

    def test_db_config(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        db = config.profiles["staging"].db
        assert db.host == "127.0.0.1"
        assert db.port == 33060
        assert db.name == "app_staging"
        assert db.user == "forge"
        assert db.password == "secret123"
        assert db.driver == "mysql"

    def test_db_dsn_mysql(self):
        db = DatabaseConfig(
            host="127.0.0.1",
            port=33060,
            name="testdb",
            user="root",
            password="pass",
            driver="mysql",
        )
        dsn = db.dsn
        assert "mysql://" in dsn
        assert "root:pass@" in dsn
        assert "127.0.0.1:33060" in dsn
        assert "testdb" in dsn

    def test_db_dsn_postgres(self):
        db = DatabaseConfig(
            host="localhost",
            port=5432,
            name="mydb",
            user="pg",
            password="",
            driver="postgres",
        )
        dsn = db.dsn
        assert "postgresql://" in dsn
        assert "mydb" in dsn

    def test_db_dsn_sqlite(self):
        db = DatabaseConfig(name="/tmp/test.db", driver="sqlite")
        assert db.dsn == "sqlite:////tmp/test.db"

    def test_profile_names(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        names = config.profile_names
        assert "staging" in names
        assert "production" in names

    def test_get_profile(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        p = config.get_profile("staging")
        assert p.name == "staging"

    def test_get_profile_missing(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        with pytest.raises(SystemExit):
            config.get_profile("nonexistent")

    def test_minimal_config(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(
            '[profiles.app]\nhost = "example.com"\n'
        )
        config = parse_config(config_file)
        assert isinstance(config, SpyroConfig)
        assert "app" in config.profiles


class TestGenerateConfig:
    def test_creates_file(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        result = generate_config(config_file)
        assert result == config_file
        assert config_file.exists()
        content = config_file.read_text()
        assert "[profiles.staging]" in content
        assert "[profiles.production]" in content

    def test_fails_if_exists(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text("existing")
        with pytest.raises(SystemExit):
            generate_config(config_file)
