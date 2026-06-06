"""Tests for service detection and smart sync exclusions."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.sync import (
    SyncPin,
    should_exclude,
    filter_files,
    detect_framework,
    load_pins,
    save_pins,
    add_pin,
    remove_pin,
    FRAMEWORK_EXCLUSIONS,
    SENSITIVE_PATTERNS,
)


class TestExclusionEngine:
    def test_env_files_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        env = base / ".env"
        env.touch()
        env_local = base / ".env.local"
        env_local.touch()
        env_prod = base / ".env.production"
        env_prod.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = set()

        assert should_exclude(env, base, files, dirs)
        assert should_exclude(env_local, base, files, dirs)
        assert should_exclude(env_prod, base, files, dirs)

    def test_normal_files_not_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        normal = base / "index.php"
        normal.touch()
        model = base / "User.php"
        model.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = set()

        assert not should_exclude(normal, base, files, dirs)
        assert not should_exclude(model, base, files, dirs)

    def test_node_modules_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        nm = base / "node_modules"
        nm.mkdir()
        pkg = nm / "lodash" / "index.js"
        pkg.parent.mkdir()
        pkg.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = {"node_modules/"}

        assert should_exclude(pkg, base, files, dirs)

    def test_vendor_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        vendor = base / "vendor"
        vendor.mkdir()
        pkg = vendor / "laravel" / "framework" / "src.php"
        pkg.parent.mkdir(parents=True)
        pkg.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = {"vendor/"}

        assert should_exclude(pkg, base, files, dirs)

    def test_include_override(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        env = base / ".env"
        env.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = set()
        includes = [".env"]

        # .env is excluded by default, but include override should allow it
        assert not should_exclude(env, base, files, dirs, includes)

    def test_log_files_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        log = base / "error.log"
        log.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = set()

        assert should_exclude(log, base, files, dirs)

    def test_swapping_files_excluded(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        swp = base / "index.php.swp"
        swp.touch()
        backup = base / "config.php~"
        backup.touch()

        files = set(SENSITIVE_PATTERNS)
        dirs = set()

        assert should_exclude(swp, base, files, dirs)
        assert should_exclude(backup, base, files, dirs)

    def test_filter_files(self, tmp_path):
        base = tmp_path / "app"
        base.mkdir()
        files_to_check = [
            base / "index.php",
            base / ".env",
            base / ".env.local",
            base / "User.php",
            base / "node_modules" / "pkg.js",
        ]
        for f in files_to_check:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.touch()

        exclude_files = set(SENSITIVE_PATTERNS)
        exclude_dirs = {"node_modules/"}

        result = filter_files(files_to_check, base, exclude_files, exclude_dirs)
        names = [f.name for f in result]

        assert "index.php" in names
        assert "User.php" in names
        assert ".env" not in names
        assert ".env.local" not in names
        assert "pkg.js" not in names


class TestFrameworkExclusions:
    def test_laravel_excludes_env(self):
        fw = FRAMEWORK_EXCLUSIONS["laravel"]
        assert ".env" in fw["files"]
        assert "vendor/" in fw["dirs"]
        assert "storage/logs/" in fw["dirs"]

    def test_wordpress_excludes_wp_config(self):
        fw = FRAMEWORK_EXCLUSIONS["wordpress"]
        assert "wp-config.php" in fw["files"]
        assert "wp-content/cache/" in fw["dirs"]

    def test_node_excludes_node_modules(self):
        fw = FRAMEWORK_EXCLUSIONS["node"]
        assert ".env" in fw["files"]
        assert "node_modules/" in fw["dirs"]

    def test_python_excludes_pycache(self):
        fw = FRAMEWORK_EXCLUSIONS["python"]
        assert "*.pyc" in fw["files"]
        assert "__pycache__/" in fw["dirs"]


class TestFrameworkDetection:
    def test_detect_laravel(self, tmp_path):
        (tmp_path / "artisan").touch()
        (tmp_path / "composer.json").touch()
        assert detect_framework(tmp_path) == "laravel"

    def test_detect_wordpress(self, tmp_path):
        (tmp_path / "wp-config.php").touch()
        assert detect_framework(tmp_path) == "wordpress"

    def test_detect_node(self, tmp_path):
        (tmp_path / "package.json").touch()
        assert detect_framework(tmp_path) == "node"

    def test_detect_python(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        assert detect_framework(tmp_path) == "python"

    def test_detect_none(self, tmp_path):
        assert detect_framework(tmp_path) == ""


class TestSyncPin:
    def test_get_all_excludes_laravel(self):
        pin = SyncPin(
            local_path="/tmp/app",
            remote_path="/var/www",
            profile="staging",
            framework="laravel",
        )
        files, dirs = pin.get_all_excludes()
        assert ".env" in files
        assert "vendor/" in dirs
        assert "node_modules/" in dirs  # Default exclusions

    def test_get_all_excludes_auto(self):
        pin = SyncPin(
            local_path="/tmp/app",
            remote_path="/var/www",
            profile="staging",
            framework="auto",
        )
        files, dirs = pin.get_all_excludes()
        # Auto applies all framework rules
        assert ".env" in files
        assert "wp-config.php" in files
        assert "vendor/" in dirs

    def test_custom_excludes_added(self):
        pin = SyncPin(
            local_path="/tmp/app",
            remote_path="/var/www",
            profile="staging",
            framework="",
            exclude_files=["*.test.js", "*.spec.ts"],
            exclude_dirs=["__tests__/"],
        )
        files, dirs = pin.get_all_excludes()
        assert "*.test.js" in files
        assert "*.spec.ts" in files
        assert "__tests__/" in dirs


class TestPinPersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.sync._pins_path", lambda: tmp_path / "pins.json")

        pin = SyncPin(
            local_path="/tmp/app",
            remote_path="/var/www",
            profile="staging",
            framework="laravel",
        )
        save_pins([pin])
        loaded = load_pins()

        assert len(loaded) == 1
        assert loaded[0].local_path == "/tmp/app"
        assert loaded[0].framework == "laravel"

    def test_add_pin(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.sync._pins_path", lambda: tmp_path / "pins.json")

        pin1 = SyncPin(local_path="/tmp/a", remote_path="/var/a", profile="staging")
        pin2 = SyncPin(local_path="/tmp/b", remote_path="/var/b", profile="staging")

        add_pin(pin1)
        add_pin(pin2)
        pins = load_pins()
        assert len(pins) == 2

    def test_add_pin_replaces_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.sync._pins_path", lambda: tmp_path / "pins.json")

        pin1 = SyncPin(local_path="/tmp/a", remote_path="/var/old", profile="staging")
        pin2 = SyncPin(local_path="/tmp/a", remote_path="/var/new", profile="staging")

        add_pin(pin1)
        add_pin(pin2)
        pins = load_pins()
        assert len(pins) == 1
        assert pins[0].remote_path == "/var/new"

    def test_remove_pin(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.sync._pins_path", lambda: tmp_path / "pins.json")

        pin = SyncPin(local_path="/tmp/a", remote_path="/var/a", profile="staging")
        add_pin(pin)
        remaining = remove_pin("/tmp/a", "staging")
        assert len(remaining) == 0
