"""Smart file sync with directory pinning and framework-aware exclusions.

Supports:
- Pin local directories to remote destinations
- Auto-sync on file save via watchdog
- Framework-aware .env and per-environment file exclusions
- Works across Laravel, WordPress, and generic projects
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Default exclusions (applied to ALL frameworks)
# ---------------------------------------------------------------------------

# Files that should NEVER be uploaded to a remote server
SENSITIVE_FILES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    ".env.development",
    ".env.testing",
    ".env.backup",
    ".env.save",
    ".env.swp",
    ".env~",
}

# Glob patterns for sensitive files
SENSITIVE_PATTERNS = [
    ".env*",            # .env and all .env.* variants
    "*.local",          # config.local.php, etc.
    "*.swp",            # vim swap files
    "*.swo",
    "*~",               # backup files
    ".DS_Store",        # macOS
    "Thumbs.db",        # Windows
    "__pycache__/",     # Python
    "node_modules/",    # Node.js
    ".git/",            # Git
    "vendor/",          # PHP Composer
    ".sass-cache/",     # Sass
    "*.log",            # Log files
]

# ---------------------------------------------------------------------------
# Framework-specific exclusion rules
# ---------------------------------------------------------------------------

FRAMEWORK_EXCLUSIONS = {
    "laravel": {
        "files": {
            ".env",
            ".env.backup",
            ".env.local",
            ".env.production",
            ".env.staging",
            ".env.testing",
            ".env.herd",
            "storage/logs/*.log",
            "storage/framework/cache/**",
            "bootstrap/cache/*.php",
        },
        "dirs": {
            "vendor/",
            "node_modules/",
            "storage/framework/cache/",
            "storage/framework/sessions/",
            "storage/framework/views/",
            "storage/logs/",
            "bootstrap/cache/",
        },
    },
    "wordpress": {
        "files": {
            ".env",
            "wp-config.php",     # Per-environment config
            "wp-config.php.bak",
            "wp-config.php.old",
            "wp-config-local.php",
            ".htaccess",         # May differ per environment
        },
        "dirs": {
            "wp-content/cache/",
            "wp-content/uploads/cache/",
            "wp-content/debug.log",
            "node_modules/",
            "vendor/",
        },
    },
    "node": {
        "files": {
            ".env",
            ".env.local",
            ".env.development.local",
            ".env.test.local",
            ".env.production.local",
            ".env*.local",
        },
        "dirs": {
            "node_modules/",
            ".next/",
            "dist/",
            "build/",
        },
    },
    "python": {
        "files": {
            ".env",
            ".env.local",
            "*.pyc",
            ".Python",
        },
        "dirs": {
            "__pycache__/",
            "*.egg-info/",
            ".venv/",
            "venv/",
            ".mypy_cache/",
            ".pytest_cache/",
        },
    },
}


# Rules that only make sense once the framework is known. Applying them to an
# unrecognised project would drop deployable files: Laravel's public/.htaccess
# and public/build/ (Vite output) would never be synced.
_ONLY_WHEN_DETECTED = {".htaccess", "build/", "dist/"}


# ---------------------------------------------------------------------------
# Pin configuration
# ---------------------------------------------------------------------------


@dataclass
class SyncPin:
    """A pinned local→remote directory sync mapping."""

    local_path: str
    remote_path: str
    profile: str
    exclude_files: list[str] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=list)
    include_patterns: list[str] = field(default_factory=list)  # Override excludes
    framework: str = ""  # auto | laravel | wordpress | node | python | ""

    def get_all_excludes(self) -> tuple[set[str], set[str]]:
        """Get combined exclusion sets (default + framework + custom)."""
        files = set(SENSITIVE_PATTERNS)
        dirs = set()

        # Add framework-specific exclusions
        if self.framework and self.framework in FRAMEWORK_EXCLUSIONS:
            fw = FRAMEWORK_EXCLUSIONS[self.framework]
            files.update(fw.get("files", set()))
            dirs.update(fw.get("dirs", set()))
        elif self.framework == "" or self.framework == "auto":
            # Unknown project: apply every framework's secrets/cache rules, but
            # not the ones that are only safe to apply when we know what it is.
            for fw in FRAMEWORK_EXCLUSIONS.values():
                files.update(fw.get("files", set()) - _ONLY_WHEN_DETECTED)
                dirs.update(fw.get("dirs", set()) - _ONLY_WHEN_DETECTED)

        # Add custom exclusions
        files.update(self.exclude_files)
        dirs.update(self.exclude_dirs)

        return files, dirs


# ---------------------------------------------------------------------------
# Exclusion engine
# ---------------------------------------------------------------------------


def should_exclude(
    file_path: Path,
    base_path: Path,
    exclude_files: set[str],
    exclude_dirs: set[str],
    include_patterns: list[str] | None = None,
) -> bool:
    """Check if a file should be excluded from sync.

    Args:
        file_path: Absolute path to the file
        base_path: Root of the sync (local_path)
        exclude_files: File patterns to exclude
        exclude_dirs: Directory patterns to exclude
        include_patterns: Override patterns (if matched, file is NOT excluded)

    Returns:
        True if the file should be skipped.
    """
    rel = file_path.relative_to(base_path)
    parts = rel.parts
    name = file_path.name

    # Check include overrides first
    if include_patterns:
        for pattern in include_patterns:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel), pattern):
                return False

    # Check directory exclusions. A pattern may name one segment ("vendor/")
    # or a path ("storage/framework/sessions/"), so test every leading path
    # prefix as well as each segment.
    dir_parts = parts[:-1]
    for i, part in enumerate(dir_parts):
        prefix = "/".join(dir_parts[: i + 1])
        for pattern in exclude_dirs:
            pattern = pattern.rstrip("/")
            if fnmatch.fnmatch(part, pattern) or fnmatch.fnmatch(prefix, pattern):
                return True
    # Patterns listed as "dirs" may also name a single file (wp-content/debug.log)
    if any(fnmatch.fnmatch(str(rel), pat.rstrip("/")) for pat in exclude_dirs):
        return True

    # Check file exclusions
    for pattern in exclude_files:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(str(rel), pattern):
            return True

    return False


def filter_files(
    files: list[Path],
    base_path: Path,
    exclude_files: set[str],
    exclude_dirs: set[str],
    include_patterns: list[str] | None = None,
) -> list[Path]:
    """Filter a list of files, removing excluded ones."""
    return [
        f for f in files
        if not should_exclude(f, base_path, exclude_files, exclude_dirs, include_patterns)
    ]


# ---------------------------------------------------------------------------
# Auto-detect framework
# ---------------------------------------------------------------------------


def detect_framework(path: Path) -> str:
    """Auto-detect the framework used in a directory.

    Returns: 'laravel', 'wordpress', 'node', 'python', or ''
    """
    indicators = {
        "laravel": ["artisan", "composer.json", "config/app.php"],
        "wordpress": ["wp-config.php", "wp-login.php", "wp-includes/"],
        "node": ["package.json", "node_modules/", ".nvmrc"],
        "python": ["setup.py", "pyproject.toml", "requirements.txt", "Pipfile"],
    }

    for framework, files in indicators.items():
        for f in files:
            if (path / f).exists():
                return framework

    return ""


# ---------------------------------------------------------------------------
# Sync state persistence
# ---------------------------------------------------------------------------

import json

_PINS_FILE = "sync_pins.json"


def _pins_path() -> Path:
    from ..utils.paths import spyro_home
    return spyro_home() / _PINS_FILE


def load_pins() -> list[SyncPin]:
    """Load pinned sync directories from ~/.spyro/sync_pins.json.

    Unknown keys (written by another spyro version) are ignored and an
    unreadable entry is skipped, so one bad pin never hides the others.
    """
    path = _pins_path()
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        try:  # keep the file; the next save must not silently overwrite it
            path.replace(path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return []
    except OSError:
        return []

    known = set(SyncPin.__dataclass_fields__)
    pins = []
    for item in data if isinstance(data, list) else []:
        try:
            pins.append(SyncPin(**{k: v for k, v in item.items() if k in known}))
        except (TypeError, AttributeError):
            continue
    return pins


def save_pins(pins: list[SyncPin]) -> None:
    """Save pinned sync directories (atomically)."""
    path = _pins_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "local_path": p.local_path,
            "remote_path": p.remote_path,
            "profile": p.profile,
            "exclude_files": p.exclude_files,
            "exclude_dirs": p.exclude_dirs,
            "include_patterns": p.include_patterns,
            "framework": p.framework,
        }
        for p in pins
    ]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def add_pin(pin: SyncPin) -> list[SyncPin]:
    """Add a sync pin (or update existing). Returns all pins."""
    pins = load_pins()
    # Remove existing pin for same local_path + profile
    pins = [
        p for p in pins
        if not (p.local_path == pin.local_path and p.profile == pin.profile)
    ]
    pins.append(pin)
    save_pins(pins)
    return pins


def remove_pin(local_path: str, profile: str) -> list[SyncPin]:
    """Remove a sync pin. Returns remaining pins."""
    pins = load_pins()
    pins = [
        p for p in pins
        if not (p.local_path == local_path and p.profile == profile)
    ]
    save_pins(pins)
    return pins
