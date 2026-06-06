"""Tests for spyro.utils — ANSI stripping, quoting, config discovery."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from src.security.ansi import strip_ansi, sanitize_output
from src.utils.paths import discover_config, ensure_private, safe_quote, spyro_home


class TestStripAnsi:
    def test_plain_text_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_strips_csi(self):
        assert strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_strips_complex_csi(self):
        assert strip_ansi("\x1b[1;32mbold green\x1b[0m") == "bold green"

    def test_strips_bytes(self):
        assert strip_ansi(b"\x1b[31mred\x1b[0m") == "red"

    def test_empty_string(self):
        assert strip_ansi("") == ""

    def test_no_escapes(self):
        assert strip_ansi("no escapes here") == "no escapes here"

    def test_multiple_sequences(self):
        text = "\x1b[31mred\x1b[0m normal \x1b[32mgreen\x1b[0m"
        assert strip_ansi(text) == "red normal green"


class TestSanitizeOutput:
    def test_plain_text(self):
        assert sanitize_output("hello") == "hello"

    def test_strips_osc(self):
        # OSC sequences (terminal title set, etc.)
        text = "\x1b]0;Title\x07"
        assert sanitize_output(text) == ""

    def test_strips_bytes(self):
        assert sanitize_output(b"\x1b[31msecret\x1b[0m") == "secret"

    def test_aggressive_stripping(self):
        # Should strip even weird sequences
        text = "\x1bP+0\x1b]test\x1b\\"
        result = sanitize_output(text)
        assert "\x1b" not in result


class TestSafeQuote:
    def test_simple_string(self):
        assert safe_quote("hello") == "hello"

    def test_with_spaces(self):
        result = safe_quote("hello world")
        assert "hello world" in result
        assert result.startswith("'") or result.startswith('"')

    def test_with_shell_metacharacters(self):
        result = safe_quote("test; rm -rf /")
        assert "rm" not in result or "'" in result or '"' in result


class TestDiscoverConfig:
    def test_finds_in_cwd(self, tmp_path):
        config = tmp_path / "spyro.toml"
        config.write_text("[profiles]\n")
        result = discover_config(tmp_path)
        assert result == config

    def test_finds_in_parent(self, tmp_path):
        child = tmp_path / "subdir" / "deeper"
        child.mkdir(parents=True)
        config = tmp_path / "spyro.toml"
        config.write_text("[profiles]\n")
        result = discover_config(child)
        assert result == config

    def test_returns_none_when_missing(self, tmp_path):
        result = discover_config(tmp_path)
        assert result is None


class TestEnsurePrivate:
    def test_sets_permissions(self, tmp_path):
        target = tmp_path / "secret.toml"
        ensure_private(target)
        assert target.exists()
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600

    def test_creates_if_missing(self, tmp_path):
        target = tmp_path / "new.toml"
        ensure_private(target)
        assert target.exists()


class TestSpyroHome:
    def test_creates_directory(self):
        home = spyro_home()
        assert home.exists()
        assert home == Path.home() / ".spyro"
