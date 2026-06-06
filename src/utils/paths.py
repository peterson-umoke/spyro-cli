"""Path helpers, shell quoting, config discovery, spyro home directory."""

from __future__ import annotations

import shlex
import stat
from pathlib import Path


# ---------------------------------------------------------------------------
# Shell quoting (shlex.quote equivalent, used everywhere)
# ---------------------------------------------------------------------------


def safe_quote(arg: str) -> str:
    """Shell-escape *arg* via shlex.quote(). Prevents injection."""
    return shlex.quote(arg)


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def ensure_private(path: Path, mode: int = 0o600) -> None:
    """Set file permissions to *mode* (default 0600). Creates if missing."""
    path.touch(exist_ok=True)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


# ---------------------------------------------------------------------------
# Config discovery (CWD walk-up)
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "spyro.toml"


def discover_config(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for spyro.toml.

    Returns the resolved Path if found, else None.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / _CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            # filesystem root
            return None
        current = parent


# ---------------------------------------------------------------------------
# Spyro home directory
# ---------------------------------------------------------------------------


def spyro_home() -> Path:
    """Return ~/.spyro, creating it if it doesn't exist."""
    home = Path.home() / ".spyro"
    home.mkdir(parents=True, exist_ok=True)
    return home
