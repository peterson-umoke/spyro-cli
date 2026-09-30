"""Tests for spyro.utils — ANSI stripping, quoting, config discovery."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from spyro.security.ansi import strip_ansi, sanitize_output
from spyro.utils.paths import discover_config, ensure_private, safe_quote, spyro_home


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


class TestSecureCredential:
    def test_handles_str_and_bytes(self):
        from spyro.security.memory import SecureCredential
        import spyro.security.memory as mem

        cred_str = SecureCredential("secret_str")
        assert cred_str.value == b"secret_str"
        cred_str.zero()
        assert cred_str.zeroed

        cred_bytes = SecureCredential(b"secret_bytes")
        assert cred_bytes.value == b"secret_bytes"
        cred_bytes.zero()
        assert cred_bytes.zeroed

        # SecureString should be deleted (YAGNI/redundant)
        assert not hasattr(mem, "SecureString")


class TestKeychainHeadless:
    def test_env_credential_per_profile_beats_global(self, monkeypatch):
        from spyro.utils.keychain import get_credential

        monkeypatch.setenv("SPYRO_PASSWORD", "global")
        monkeypatch.setenv("SPYRO_PASSWORD_MY_STAGING", "specific")
        assert get_credential("my-staging", "deploy") == "specific"
        assert get_credential("other", "deploy") == "global"

    def test_no_prompt_when_stdin_is_not_a_tty(self, monkeypatch):
        import getpass
        from spyro.utils import keychain

        monkeypatch.delenv("SPYRO_PASSWORD", raising=False)
        monkeypatch.setattr(keychain, "get_credential", lambda *a: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        monkeypatch.setattr(getpass, "getpass", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
        assert keychain.prompt_for_credential("p", "u") == ""


class TestSocketsDir:
    def test_short_home_uses_spyro_sockets(self, tmp_path, monkeypatch):
        from spyro.utils import paths

        monkeypatch.setattr(paths, "spyro_home", lambda: tmp_path / "h")
        monkeypatch.setattr(paths, "_MAX_SOCKET_DIR", 10_000)
        d = paths.sockets_dir()
        assert d == tmp_path / "h" / "sockets" and oct(d.stat().st_mode & 0o777) == "0o700"

    def test_long_home_falls_back_to_a_short_private_dir(self, tmp_path, monkeypatch):
        from spyro.utils import paths

        monkeypatch.setattr(paths, "spyro_home", lambda: tmp_path / ("x" * 60))
        d = paths.sockets_dir()
        try:
            assert d == Path(f"/tmp/spyro-{os.getuid()}")
            assert len(f"{d}/{'0' * 40}.{'a' * 16}") < 104        # fits macOS's sun_path
            assert oct(d.stat().st_mode & 0o777) == "0o700"
        finally:
            try:
                d.rmdir()  # only removes it if empty (nothing else is using it)
            except OSError:
                pass

    def test_chosen_dir_always_fits_the_socket_limit(self, tmp_path, monkeypatch):
        from spyro.utils import paths

        for home in (tmp_path / "h", tmp_path / ("y" * 80)):
            monkeypatch.setattr(paths, "spyro_home", lambda home=home: home)
            d = paths.sockets_dir()
            assert len(f"{d}/{'0' * 40}.{'a' * 16}") < 104
            if str(d).startswith("/tmp/spyro-"):
                try:
                    d.rmdir()
                except OSError:
                    pass
