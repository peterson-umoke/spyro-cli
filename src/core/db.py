"""Database credential resolution with dual strategy.

1. Explicit TOML Definition: Use credentials from spyro.toml
2. Automatic Detection Fallback: Parse remote .env / config files
"""

from __future__ import annotations

import re
from typing import Optional

from ..utils.config import DatabaseConfig, ProfileConfig
from ..core.pty_engine import PTYRunner, build_ssh_args
from ..utils.paths import safe_quote


# ---------------------------------------------------------------------------
# Remote .env parser
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(
    r"""
    ^(?:export\s+)?        # optional export keyword
    ([A-Z_][A-Z0-9_]*)     # variable name
    \s*=\s*                 # equals sign
    (?:
        "([^"]*)"           # double-quoted value
      | '([^']*)'           # single-quoted value
      | ([^\s#]*)           # unquoted value (until space or comment)
    )
    """,
    re.MULTILINE | re.VERBOSE,
)

# Laravel .env DB_* patterns
_LARAVEL_DB_MAP = {
    "DB_HOST": "host",
    "DB_PORT": "port",
    "DB_DATABASE": "name",
    "DB_USERNAME": "user",
    "DB_PASSWORD": "password",
    "DB_CONNECTION": "driver",
}


def _parse_env_file(content: str) -> dict[str, str]:
    """Parse a .env file content into a dict."""
    result = {}
    for match in _ENV_VAR_RE.finditer(content):
        var_name = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4)
        result[var_name] = value
    return result


def _env_to_db_config(env_vars: dict[str, str]) -> DatabaseConfig:
    """Convert Laravel-style DB_* env vars to DatabaseConfig."""
    db = DatabaseConfig()

    for env_key, db_field in _LARAVEL_DB_MAP.items():
        value = env_vars.get(env_key, "")
        if value:
            if db_field == "port":
                try:
                    db.port = int(value)
                except ValueError:
                    pass
            elif db_field == "driver":
                db.driver = value
            else:
                setattr(db, db_field, value)

    return db


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_db_credentials(
    profile: ProfileConfig,
    *,
    runner: PTYRunner | None = None,
    password: str = "",
    local_override: DatabaseConfig | None = None,
) -> DatabaseConfig:
    """Resolve database credentials using dual strategy.

    Priority:
    1. local_override (explicit CLI argument)
    2. TOML-defined credentials (non-empty password)
    3. Remote .env / config file detection
    """
    # 1. Local override takes precedence
    if local_override and local_override.password:
        return local_override

    # 2. TOML-defined credentials
    db = profile.db
    if db.password:
        return db

    # 3. Remote detection fallback
    return _detect_remote_credentials(profile, runner=runner, password=password)


def _detect_remote_credentials(
    profile: ProfileConfig,
    *,
    runner: PTYRunner | None = None,
    password: str = "",
) -> DatabaseConfig:
    """Detect database credentials from remote config files."""
    if not runner:
        runner = PTYRunner()

    ssh_args = build_ssh_args(
        host=profile.host,
        user=profile.user,
        port=profile.port,
        key=profile.key,
    )

    for env_file in profile.env_files:
        remote_path = f"{profile.remote_path}/{env_file}"
        cmd = ssh_args + [f"cat {safe_quote(remote_path)} 2>/dev/null"]

        output_lines: list[str] = []

        def collect(line: str) -> None:
            output_lines.append(line)

        exit_code = runner.run(
            cmd,
            password=password,
            on_output=collect,
            timeout=15.0,
        )

        if exit_code == 0 and output_lines:
            content = "\n".join(output_lines)
            env_vars = _parse_env_file(content)

            if env_vars:
                db = _env_to_db_config(env_vars)
                if db.name:
                    return db

    return profile.db


def generate_connection_url(db: DatabaseConfig, port_override: int | None = None) -> str:
    """Generate a standard connection URL for database GUIs."""
    port = port_override or db.port
    if db.driver == "sqlite":
        return f"sqlite:///{db.name}"
    scheme = {"mysql": "mysql", "postgres": "postgresql"}.get(db.driver, db.driver)
    auth = f"{db.user}:{db.password}@" if db.user else ""
    return f"{scheme}://{auth}127.0.0.1:{port}/{db.name}"
