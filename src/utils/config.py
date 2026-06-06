"""Configuration discovery, parsing, and validation for spyro.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.paths import discover_config, ensure_private, spyro_home

# tomllib for Python 3.11+
import tomllib


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DatabaseConfig:
    """Database connection details for a profile."""

    host: str = "127.0.0.1"
    port: int = 3306
    name: str = ""
    user: str = ""
    password: str = ""
    driver: str = "mysql"  # mysql | postgres | sqlite

    @property
    def dsn(self) -> str:
        if self.driver == "sqlite":
            return f"sqlite:///{self.name}"
        scheme = {"mysql": "mysql", "postgres": "postgresql"}.get(
            self.driver, self.driver
        )
        auth = f"{self.user}:{self.password}@" if self.user else ""
        return f"{scheme}://{auth}{self.host}:{self.port}/{self.name}"


@dataclass
class ProfileConfig:
    """A single environment profile (staging, production, etc.)."""

    name: str
    host: str
    user: str = "deploy"
    port: int = 22
    key: str = ""  # path to SSH key (optional)
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    remote_path: str = "/var/www"
    forwarded_ports: list[int] = field(default_factory=list)
    artisan: bool = False  # detect Laravel artisan
    wordpress: bool = False  # detect WordPress / WP-CLI
    wp_cli_path: str = ""  # custom path to wp-cli (default: auto-detect)
    sudo: bool = False  # whether sudo is available
    env_files: list[str] = field(default_factory=lambda: [".env"])
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpyroConfig:
    """Top-level spyro.toml configuration."""

    profiles: dict[str, ProfileConfig] = field(default_factory=dict)
    global_settings: dict[str, Any] = field(default_factory=dict)
    config_path: Path | None = None

    @property
    def profile_names(self) -> list[str]:
        return list(self.profiles.keys())

    def get_profile(self, name: str) -> ProfileConfig:
        if name not in self.profiles:
            available = ", ".join(self.profile_names) or "(none)"
            raise SystemExit(
                f"Profile '{name}' not found. Available: {available}"
            )
        return self.profiles[name]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_db(raw: dict[str, Any] | None) -> DatabaseConfig:
    if not raw:
        return DatabaseConfig()
    return DatabaseConfig(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 3306)),
        name=raw.get("name", ""),
        user=raw.get("user", ""),
        password=raw.get("password", ""),
        driver=raw.get("driver", "mysql"),
    )


def _parse_profile(name: str, raw: dict[str, Any]) -> ProfileConfig:
    return ProfileConfig(
        name=name,
        host=raw["host"],
        user=raw.get("user", "deploy"),
        port=int(raw.get("port", 22)),
        key=raw.get("key", ""),
        db=_parse_db(raw.get("db")),
        remote_path=raw.get("remote_path", "/var/www"),
        forwarded_ports=raw.get("forwarded_ports", []),
        artisan=raw.get("artisan", False),
        wordpress=raw.get("wordpress", False),
        wp_cli_path=raw.get("wp_cli_path", ""),
        sudo=raw.get("sudo", False),
        env_files=raw.get("env_files", [".env"]),
        extra={
            k: v
            for k, v in raw.items()
            if k
            not in {
                "host",
                "user",
                "port",
                "key",
                "db",
                "remote_path",
                "forwarded_ports",
                "artisan",
                "wordpress",
                "wp_cli_path",
                "sudo",
                "env_files",
            }
        },
    )


def parse_config(path: Path) -> SpyroConfig:
    """Parse a spyro.toml file and return SpyroConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    profiles: dict[str, ProfileConfig] = {}
    raw_profiles = raw.get("profiles", {})
    for name, data in raw_profiles.items():
        profiles[name] = _parse_profile(name, data)

    # Top-level settings that aren't profiles
    global_settings = {
        k: v for k, v in raw.items() if k != "profiles"
    }

    return SpyroConfig(
        profiles=profiles,
        global_settings=global_settings,
        config_path=path,
    )


def load_config(start: Path | None = None) -> SpyroConfig:
    """Discover and load spyro.toml from *start* upward.

    Returns SpyroConfig or exits with an error message.
    """
    path = discover_config(start)
    if path is None:
        raise SystemExit(
            "No spyro.toml found. Run 'spyro init' to create one."
        )
    config = parse_config(path)

    # Enforce 0600 if config contains passwords
    has_secrets = any(
        p.db.password for p in config.profiles.values()
    )
    if has_secrets:
        ensure_private(path)

    return config


# ---------------------------------------------------------------------------
# Template generation for `spyro init`
# ---------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# Spyro Configuration
# Docs: https://github.com/yourorg/spyro

[profiles.staging]
host = "staging.example.com"
user = "deploy"
port = 22
# key = "~/.ssh/id_ed25519"
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [33060, 63790]

[profiles.staging.db]
host = "127.0.0.1"
port = 33060
name = "app_staging"
user = "forge"
password = ""
driver = "mysql"

[profiles.production]
host = "production.example.com"
user = "deploy"
port = 22
remote_path = "/var/www/app"
artisan = true
sudo = false
forwarded_ports = [33061]

[profiles.production.db]
host = "127.0.0.1"
port = 33061
name = "app_production"
user = "forge"
password = ""
driver = "mysql"

# WordPress profile example
# [profiles.wordpress]
# host = "wp.example.com"
# user = "deploy"
# remote_path = "/var/www/html"
# wordpress = true
# sudo = false
# forwarded_ports = [33062]
#
# [profiles.wordpress.db]
# host = "127.0.0.1"
# port = 33062
# name = "wordpress"
# user = "wp_user"
# password = ""
# driver = "mysql"
"""


def generate_config(dest: Path | None = None) -> Path:
    """Write a default spyro.toml template. Returns the path written."""
    target = dest or discover_config() or (Path.cwd() / "spyro.toml")
    if target.exists():
        raise SystemExit(f"Configuration already exists at {target}")
    target.write_text(CONFIG_TEMPLATE)
    ensure_private(target)
    return target
