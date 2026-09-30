"""Database credential resolution with dual strategy.

1. Explicit TOML Definition: Use credentials from spyro.toml
2. Automatic Detection Fallback: Parse remote .env / config files
"""

from __future__ import annotations

import os
import re

from ..utils.config import DatabaseConfig, ProfileConfig
from ..core.pty_engine import PTYRunner, build_ssh_args
from ..utils.paths import safe_quote


# ---------------------------------------------------------------------------
# Remote .env parser
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(
    r"""
    ^[ \t]*(?:export[ \t]+)?     # optional export keyword
    ([A-Za-z_][A-Za-z0-9_]*)      # variable name
    [ \t]*=[ \t]*
    (?:
        "((?:[^"\\]|\\.)*)"[ \t]*(?:\#.*)?$   # double-quoted, \" and \\ escapes
      | '([^']*)'[ \t]*(?:\#.*)?$                # single-quoted
      | ([^\r\n]*?)(?:[ \t]+\#.*)?[ \t]*$        # unquoted: a '#' only starts a comment after whitespace
    )
    """,
    re.MULTILINE | re.VERBOSE,
)

_DRIVERS = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "pgsql": "postgres",
    "postgres": "postgres",
    "postgresql": "postgres",
    "sqlite": "sqlite",
}

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
    for match in _ENV_VAR_RE.finditer(content.replace("\r\n", "\n").replace("\r", "\n")):
        var_name = match.group(1)
        double, single, bare = match.group(2), match.group(3), match.group(4)
        if double is not None:
            value = re.sub(r'\\(["\\$])', r"\1", double)
        else:
            value = single if single is not None else (bare or "")
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
                db.driver = _DRIVERS.get(value.lower(), value)
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


def fetch_remote_file(
    profile: ProfileConfig,
    path: str,
    *,
    runner: PTYRunner | None = None,
    password: str = "",
    timeout: float = 15.0,
) -> str | None:
    """``cat`` a file on the profile's server. Returns its text, or None on failure."""
    runner = runner or PTYRunner()
    ssh_args = build_ssh_args(host=profile.host, user=profile.user, port=profile.port, key=profile.key)
    lines: list[str] = []
    exit_code = runner.run(
        ssh_args + [f"cat -- {safe_quote(path)}"],
        password=password,
        on_output=lines.append,
        timeout=timeout,
    )
    if exit_code != 0:
        return None
    return "\n".join(lines).rstrip("\n") + "\n"


def _detect_remote_credentials(
    profile: ProfileConfig,
    *,
    runner: PTYRunner | None = None,
    password: str = "",
) -> DatabaseConfig:
    """Detect database credentials from remote config files."""
    runner = runner or PTYRunner()

    for env_file in profile.env_files:
        content = fetch_remote_file(
            profile, f"{profile.remote_path}/{env_file}", runner=runner, password=password
        )
        if content:
            db = _env_to_db_config(_parse_env_file(content))
            if db.name:
                return db

    return profile.db


# ---------------------------------------------------------------------------
# Local database clients (through the tunnel)
# ---------------------------------------------------------------------------


def client_env(db: DatabaseConfig, port: int) -> dict[str, str]:
    """Environment for a local DB client. The password travels in the
    environment (MYSQL_PWD / PGPASSWORD), never on the command line where any
    local user could read it with ``ps``."""
    env = os.environ.copy()
    if db.password:
        env["MYSQL_PWD"] = db.password
        env["PGPASSWORD"] = db.password
    env.update({"PGHOST": "127.0.0.1", "PGPORT": str(port), "PGUSER": db.user,
                "PGDATABASE": db.name or "postgres"})
    return env


def client_argv(client: str, db: DatabaseConfig, port: int, query: str | None = None) -> list[str]:
    """Argument list for mysql/mariadb/psql against ``127.0.0.1:port``."""
    if client == "psql":
        return ["psql"] + (["-c", query] if query else [])
    argv = [client, "-h127.0.0.1", f"-P{port}", f"-u{db.user}", "--skip-ssl", db.name]
    return argv + (["-e", query] if query else [])


def generate_connection_url(db: DatabaseConfig, port_override: int | None = None) -> str:
    """Generate a standard connection URL for database GUIs."""
    port = port_override or db.port
    if db.driver == "sqlite":
        return f"sqlite:///{db.name}"
    scheme = {"mysql": "mysql", "postgres": "postgresql"}.get(db.driver, db.driver)
    auth = f"{db.user}:{db.password}@" if db.user else ""
    return f"{scheme}://{auth}127.0.0.1:{port}/{db.name}"
