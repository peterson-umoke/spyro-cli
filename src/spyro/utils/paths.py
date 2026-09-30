"""Path helpers, shell quoting, config discovery, spyro home directory."""

from __future__ import annotations

import os
import shlex
import stat
from pathlib import Path


# ---------------------------------------------------------------------------
# Shell quoting (shlex.quote equivalent, used everywhere)
# ---------------------------------------------------------------------------


safe_quote = shlex.quote


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def ensure_private(path: Path, mode: int = 0o600) -> None:
    """Set file permissions to *mode* (default 0600). Creates if missing."""
    path.touch(exist_ok=True)
    path.chmod(mode)


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


# ssh appends ".<16 random chars>" to a ControlPath while binding it, and a
# Unix socket path is limited to 104 bytes on macOS (108 on Linux). The longest
# file name we put in the directory is "<40 hex>" (ssh's %C token), so the
# directory must stay under 104 - 1 - 40 - 17 - 1 (NUL) = 45 bytes.
_MAX_SOCKET_DIR = 45


def sockets_dir() -> Path:
    """Directory for ssh ControlMaster sockets (created, owner-only).

    ``~/.spyro/sockets`` when that path is short enough; otherwise
    ``/tmp/spyro-<uid>`` (a long home directory would make every ssh command
    fail with "too long for Unix domain socket").
    """
    preferred = spyro_home() / "sockets"
    if len(str(preferred)) <= _MAX_SOCKET_DIR:
        path = preferred
    else:
        path = Path(f"/tmp/spyro-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():  # someone pre-created it in shared /tmp
        raise RuntimeError(f"{path} is owned by another user; refusing to put ssh sockets there")
    path.chmod(0o700)
    return path
