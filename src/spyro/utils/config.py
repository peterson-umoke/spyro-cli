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
# SSH config parsing
# ---------------------------------------------------------------------------


SSH_CONFIG_PATH = Path.home() / ".ssh" / "config"


def parse_ssh_config(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Parse ~/.ssh/config into a {host_pattern: {settings}} mapping.

    Returns a dict where keys are Host patterns (e.g. "myserver", "*")
    and values are dicts with optional keys: hostname, user, port, identityfile.
    Wildcard patterns (``*``) are included but must be matched specifically.
    """
    result: dict[str, dict[str, str]] = {}
    cfg = path or SSH_CONFIG_PATH

    if not cfg.exists() or not cfg.is_file():
        return result

    current_hosts: list[str] = []
    current_block: dict[str, str] = {}

    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, PermissionError):
        return result

    for raw_line in text.splitlines():
        line = raw_line.strip()
        # Skip comments and blank lines
        if not line or line.startswith("#"):
            continue

        # Host directive starts a new block
        if line.lower().startswith("host "):
            # Save previous block
            if current_hosts and current_block:
                for h in current_hosts:
                    result.setdefault(h, {}).update(current_block)
            current_hosts = line[5:].strip().split()
            current_block = {}
            continue

        if not current_hosts:
            continue

        # Parse key-value pairs
        if "=" in line:
            key, _, val = line.partition("=")
        else:
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            key, val = parts

        key = key.strip().lower()
        val = val.strip()

        if key == "hostname":
            current_block["hostname"] = val
        elif key == "user":
            current_block["user"] = val
        elif key == "port":
            current_block["port"] = val
        elif key == "identityfile":
            current_block["identityfile"] = val
        elif key == "host":
            # Already handled above
            pass

    # Save last block
    if current_hosts and current_block:
        for h in current_hosts:
            result.setdefault(h, {}).update(current_block)

    return result


def resolve_ssh_for_profile(profile_name: str) -> dict[str, str]:
    """Look up SSH config settings for a profile name.

    Checks exact Host matches first, then wildcard ``*``.
    Returns a dict with optional keys: hostname, user, port, identityfile.
    """
    ssh_cfg = parse_ssh_config()

    # 1. Exact match on profile name
    if profile_name in ssh_cfg:
        return dict(ssh_cfg[profile_name])

    # 2. Wildcard fallback
    if "*" in ssh_cfg:
        return dict(ssh_cfg["*"])

    return {}


def apply_ssh_to_profile(profile: ProfileConfig) -> None:
    """Mutate a ProfileConfig in-place, inheriting SSH config settings.

    If the profile's ``host`` value matches an SSH Host entry, the
    SSH config's HostName replaces the profile host, and User/Port/
    IdentityFile fill in any gaps.
    """
    ssh_settings = resolve_ssh_for_profile(profile.name)
    if not ssh_settings:
        # Try matching profile host against SSH Host entries
        ssh_settings = resolve_ssh_for_profile(profile.host)
    if not ssh_settings:
        return

    # HostName from SSH config overrides the profile's host
    if "hostname" in ssh_settings:
        profile.host = ssh_settings["hostname"]
    # Only inherit user/port/key if NOT explicitly set in profile
    if "user" in ssh_settings and profile.user == "deploy":
        profile.user = ssh_settings["user"]
    if "port" in ssh_settings and profile.port == 22:
        profile.port = int(ssh_settings["port"])
    if "identityfile" in ssh_settings and not profile.key:
        profile.key = ssh_settings["identityfile"]


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

    config = SpyroConfig(
        profiles=profiles,
        global_settings=global_settings,
        config_path=path,
    )

    # Apply SSH config inheritance to profiles
    for profile in config.profiles.values():
        apply_ssh_to_profile(profile)

    return config


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


def resolve_profile(profile: str | None) -> str:
    """Resolve profile name: use provided, or auto-detect if only one exists.

    Priority:
    1. Explicit ``-p`` argument
    2. ``[defaults] profile = "..."`` in spyro.toml
    3. Auto-detect if only one profile exists

    Raises:
        SystemExit: If no profile specified and multiple profiles exist
    """
    config = load_config()

    if profile:
        # Explicit profile provided — validate it exists
        config.get_profile(profile)  # Raises if not found
        return profile

    # Check [defaults] section for a default profile
    defaults = config.global_settings.get("defaults", {})
    if isinstance(defaults, dict) and "profile" in defaults:
        default_profile = defaults["profile"]
        config.get_profile(default_profile)  # validate exists
        return default_profile

    # No profile specified — check if there's only one
    if len(config.profile_names) == 1:
        return config.profile_names[0]

    # Multiple profiles — require -p
    available = ", ".join(config.profile_names)
    raise SystemExit(
        f"Multiple profiles found ({available}). "
        f"Use -p <profile> to specify which one."
    )


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
