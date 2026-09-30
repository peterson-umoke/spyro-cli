"""Tests for spyro.config — TOML parsing, validation, template generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from spyro.utils.config import (
    DatabaseConfig,
    ProfileConfig,
    SpyroConfig,
    generate_config,
    parse_config,
)


SAMPLE_WP_TOML = """\
[profiles.wp]
host = "wp.example.com"
user = "deploy"
remote_path = "/var/www/html"
wordpress = true
wp_cli_path = "/usr/local/bin/wp"
sudo = false
forwarded_ports = [33062]

[profiles.wp.db]
host = "127.0.0.1"
port = 33062
name = "wordpress"
user = "wp_user"
password = ""
driver = "mysql"
"""

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


class TestWordPressConfig:
    def test_wordpress_fields(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_WP_TOML)
        config = parse_config(config_file)
        p = config.profiles["wp"]
        assert p.wordpress is True
        assert p.wp_cli_path == "/usr/local/bin/wp"
        assert p.host == "wp.example.com"
        assert p.remote_path == "/var/www/html"

    def test_wordpress_default_false(self, tmp_path):
        config_file = tmp_path / "spyro.toml"
        config_file.write_text(SAMPLE_TOML)
        config = parse_config(config_file)
        p = config.profiles["staging"]
        assert p.wordpress is False
        assert p.wp_cli_path == ""


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


class TestSshConfigInheritance:
    """`Host *` must not shadow alias lookup or override explicit TOML values."""

    @staticmethod
    def _ssh(tmp_path, monkeypatch, text):
        import spyro.utils.config as cfg

        path = tmp_path / "ssh_config"
        path.write_text(text)
        monkeypatch.setattr(cfg, "SSH_CONFIG_PATH", path)
        return cfg

    def test_alias_block_wins_over_wildcard(self, tmp_path, monkeypatch):
        cfg = self._ssh(tmp_path, monkeypatch,
                        "Host myalias\n  HostName 10.0.0.5\n  User alice\n\nHost *\n  User bob\n")
        p = cfg.ProfileConfig(name="staging", host="myalias")
        cfg.apply_ssh_to_profile(p)
        assert (p.host, p.user) == ("10.0.0.5", "alice")

    def test_wildcard_never_supplies_hostname(self, tmp_path, monkeypatch):
        cfg = self._ssh(tmp_path, monkeypatch, "Host *\n  HostName evil.example.com\n  User bob\n")
        p = cfg.ProfileConfig(name="staging", host="staging.example.com")
        cfg.apply_ssh_to_profile(p)
        assert p.host == "staging.example.com"
        assert p.user == "bob"

    def test_explicit_default_looking_user_is_kept(self, tmp_path, monkeypatch):
        cfg = self._ssh(tmp_path, monkeypatch, "Host *\n  User bob\n")
        p = cfg.ProfileConfig(name="s", host="h", user="deploy")
        cfg.apply_ssh_to_profile(p, explicit={"host", "user"})
        assert p.user == "deploy"

    def test_glob_host_pattern_matches(self, tmp_path, monkeypatch):
        cfg = self._ssh(tmp_path, monkeypatch, "Host *.internal\n  User ops\n  Port 2222\n")
        p = cfg.ProfileConfig(name="db", host="db1.internal")
        cfg.apply_ssh_to_profile(p)
        assert (p.user, p.port) == ("ops", 2222)


class TestConfigErrors:
    def test_missing_host_is_a_message_not_a_traceback(self, tmp_path):
        import pytest
        from spyro.utils.config import parse_config

        f = tmp_path / "spyro.toml"
        f.write_text('[profiles.a]\nuser = "x"\n')
        with pytest.raises(SystemExit) as e:
            parse_config(f)
        assert "'host' is required" in str(e.value) and "'a'" in str(e.value)

    def test_invalid_toml_is_a_message(self, tmp_path):
        import pytest
        from spyro.utils.config import parse_config

        f = tmp_path / "spyro.toml"
        f.write_text("[profiles.a\nhost = 1")
        with pytest.raises(SystemExit) as e:
            parse_config(f)
        assert "Invalid TOML" in str(e.value)

    def test_bad_port_is_a_message(self, tmp_path):
        import pytest
        from spyro.utils.config import parse_config

        f = tmp_path / "spyro.toml"
        f.write_text('[profiles.a]\nhost = "h"\nport = "abc"\n')
        with pytest.raises(SystemExit) as e:
            parse_config(f)
        assert "invalid value" in str(e.value)

    def test_sudo_user_is_parsed(self, tmp_path):
        from spyro.utils.config import parse_config

        f = tmp_path / "spyro.toml"
        f.write_text('[profiles.a]\nhost = "h"\nsudo = true\nsudo_user = "www-data"\n')
        p = parse_config(f).profiles["a"]
        assert p.sudo_user == "www-data" and "sudo_user" not in p.extra

    def test_generated_template_forwards_the_db_port(self, tmp_path):
        from spyro.utils.config import generate_config, parse_config

        cfg = parse_config(generate_config(tmp_path / "spyro.toml"))
        for p in cfg.profiles.values():
            assert p.db.port in p.forwarded_ports
