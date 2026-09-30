"""Tests for spyro.core.services — service status models and detection."""

from __future__ import annotations
from unittest.mock import patch

import pytest
from spyro.core.services import (
    ServiceStatus,
    detect_redis,
    detect_supervisor,
    detect_php_fpm,
    detect_php,
    detect_apache,
    detect_nginx,
    detect_caddy,
    detect_nodejs,
    detect_npm,
    detect_all_services,
)


class TestServiceStatus:
    def test_icon_and_summary(self):
        s = ServiceStatus(name="Redis", available=False)
        assert "✗" in s.icon
        assert s.summary == "Redis"

        s2 = ServiceStatus(name="Redis", available=True, running=True, version="7.0.0")
        assert "✓" in s2.icon
        assert s2.summary == "Redis v7.0.0 running"

        s3 = ServiceStatus(name="Redis", available=True, running=False)
        assert "⚠" in s3.icon
        assert s3.summary == "Redis installed (not running)"


class TestServiceDetectors:
    @patch("spyro.core.services._run_check")
    def test_detect_redis_not_found(self, mock_run):
        mock_run.return_value = (1, "")
        status = detect_redis(["ssh", "dummy"])
        assert status.name == "Redis"
        assert status.available is False

    @patch("spyro.core.services._run_check")
    def test_detect_redis_running(self, mock_run):
        def fake_run(ssh_args, cmd, timeout=3):
            if "command -v redis-server" in cmd:
                return 0, "/usr/bin/redis-server"
            if "--version" in cmd:
                return 0, "Redis server v=7.2.4 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=1234"
            if "pgrep" in cmd:
                return 0, "1234"
            if "redis-cli info" in cmd:
                return 0, "redis_version:7.2.4\ntcp_port:6379\nos:Linux"
            return 1, ""

        mock_run.side_effect = fake_run
        status = detect_redis(["ssh", "dummy"])
        assert status.available is True
        assert status.running is True
        assert status.version == "7.2.4"
        assert status.details.get("port") == "6379"

    @patch("spyro.core.services._run_check", return_value=(0, ""))
    @patch("spyro.core.services.detect_redis")
    @patch("spyro.core.services.detect_supervisor")
    @patch("spyro.core.services.detect_php_fpm")
    @patch("spyro.core.services.detect_php")
    @patch("spyro.core.services.detect_apache")
    @patch("spyro.core.services.detect_nginx")
    @patch("spyro.core.services.detect_caddy")
    @patch("spyro.core.services.detect_nodejs")
    @patch("spyro.core.services.detect_npm")
    def test_detect_all_services(self, *mocks):
        for m in mocks[:-1]:
            m.return_value = ServiceStatus(name="mock")
        results = detect_all_services("example.com")
        assert len(results) == 9


class TestUnreachableHost:
    @patch("spyro.core.services._run_check")
    def test_unreachable_host_is_one_error_not_nine_timeouts(self, mock_run):
        mock_run.return_value = (-1, "timeout")
        with pytest.raises(ConnectionError, match="timeout"):
            detect_all_services("10.255.255.1")
        assert mock_run.call_count == 1  # no per-service probing

    @patch("spyro.core.services._run_check")
    def test_timeout_is_not_reported_as_not_installed(self, mock_run):
        mock_run.return_value = (-1, "timeout")
        status = detect_redis(["ssh", "dummy"])
        assert status.available is False
        assert status.error == "timeout"
        assert "not found" not in status.error
