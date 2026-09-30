"""Remote service detection for spyro doctor.

Detects: Redis, Supervisor, PHP-FPM, Node.js/npm, PHP, Apache, Nginx, Caddy.
Each detector runs a lightweight SSH check and returns structured results.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from ..core.pty_engine import build_ssh_args


@dataclass
class ServiceStatus:
    """Result of a remote service detection check."""

    name: str
    available: bool = False
    running: bool = False
    version: str = ""
    path: str = ""
    details: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def icon(self) -> str:
        if not self.available:
            return "[red]✗[/red]"
        if self.running:
            return "[green]✓[/green]"
        return "[yellow]⚠[/yellow]"

    @property
    def summary(self) -> str:
        parts = [self.name]
        if self.version:
            parts.append(f"v{self.version}")
        if self.running:
            parts.append("running")
        elif self.available:
            parts.append("installed (not running)")
        return " ".join(parts)


def _run_check(ssh_args: list[str], cmd: str, timeout: int = 8) -> tuple[int, str]:
    """Run a command on the remote server via SSH.

    Returns ``(-1, reason)`` when ssh itself could not run or timed out.
    """
    try:
        res = subprocess.run(ssh_args + [cmd], capture_output=True, timeout=timeout)
        out = res.stdout.decode(errors="replace").strip()
        if res.returncode == 255:  # ssh's own failure code: surface its message
            err = res.stderr.decode(errors="replace").strip().splitlines()
            out = err[-1] if err else out
        return res.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


def _get_version(ssh_args: list[str], cmd: str) -> str:
    """Extract version string from a remote command."""
    rc, output = _run_check(ssh_args, cmd)
    if rc == 0 and output:
        line = output.splitlines()[0].strip()
        for word in line.split():
            if any(c.isdigit() for c in word):
                ver = word.lstrip("vV")
                if "." in ver:
                    return ver
        return line[:50]
    return ""


def _detect_service(
    ssh_args: list[str],
    name: str,
    bin_candidates: list[str],
    proc_pattern: str,
    ver_cmd: str | None = None,
    ver_parser: Callable[[str], str] | None = None,
    extra_fn: Callable[[list[str], ServiceStatus], None] | None = None,
) -> ServiceStatus:
    status = ServiceStatus(name=name)
    find_cmd = " || ".join(f"command -v {c} 2>/dev/null" for c in bin_candidates)
    rc, path = _run_check(ssh_args, find_cmd)
    if rc == 0 and path:
        status.available = True
        status.path = path.splitlines()[0].strip()
    else:
        # rc -1/255 means we never reached the host (timeout, refused, auth):
        # that is not the same as "not installed".
        status.error = path if rc in (-1, 255) else f"{bin_candidates[0]} not found"
        return status

    vcmd = ver_cmd or f"{status.path} --version 2>/dev/null"
    if ver_parser:
        rc, v_out = _run_check(ssh_args, vcmd)
        if rc == 0 and v_out:
            status.version = ver_parser(v_out)
    else:
        status.version = _get_version(ssh_args, vcmd)

    rc, _ = _run_check(ssh_args, f"pgrep {proc_pattern} >/dev/null 2>&1")
    status.running = rc == 0

    if status.running and extra_fn:
        extra_fn(ssh_args, status)

    return status


def detect_redis(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, info = _run_check(args, "timeout 5 redis-cli info server 2>/dev/null | grep -E 'redis_version|tcp_port|os'", timeout=5)
        if rc == 0 and info:
            for line in info.splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    k, v = k.strip(), v.strip()
                    if k == "redis_version":
                        s.version = v
                    elif k == "tcp_port":
                        s.details["port"] = v
                    elif k == "os":
                        s.details["os"] = v

    return _detect_service(
        ssh_args, "Redis", ["redis-server", "/usr/bin/redis-server", "/usr/local/bin/redis-server", "/opt/redis/bin/redis-server"],
        proc_pattern="-x redis-server", extra_fn=_extra
    )


def detect_supervisor(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, procs = _run_check(args, f"{s.path} status 2>/dev/null | head -20", timeout=5)
        if rc == 0 and procs:
            s.details["running_processes"] = str(procs.lower().count("running"))
            s.details["stopped_processes"] = str(procs.lower().count("stopped"))

    return _detect_service(
        ssh_args, "Supervisor", ["supervisorctl", "/usr/bin/supervisorctl", "/usr/local/bin/supervisorctl"],
        proc_pattern="-x supervisord >/dev/null 2>&1 || pgrep -f 'python.*supervisord'", extra_fn=_extra
    )


def detect_php_fpm(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, pools = _run_check(args, "php-fpm -tt 2>/dev/null | grep '\\[pool' | wc -l || echo 0", timeout=5)
        if rc == 0 and pools.strip().isdigit():
            s.details["pools"] = pools.strip()

    return _detect_service(
        ssh_args, "PHP-FPM",
        ["php-fpm", "php-fpm8.3", "php-fpm8.2", "php-fpm8.1", "php-fpm8.0", "php-fpm7.4", "php8.3-fpm", "php8.2-fpm", "php8.1-fpm", "php8.0-fpm", "php7.4-fpm", "php"],
        proc_pattern="-f 'php-fpm.*master' >/dev/null 2>&1 || pgrep -x php-fpm",
        ver_cmd="php -v 2>/dev/null | head -1",
        ver_parser=lambda out: out.split()[1] if len(out.split()) >= 2 else "",
        extra_fn=_extra
    )


def detect_nodejs(ssh_args: list[str]) -> ServiceStatus:
    return _detect_service(
        ssh_args, "Node.js", ["node", "/usr/bin/node", "/usr/local/bin/node", "/opt/node/bin/node"],
        proc_pattern="-x node >/dev/null 2>&1 || pgrep -f 'node '"
    )


def detect_npm(ssh_args: list[str]) -> ServiceStatus:
    return _detect_service(
        ssh_args, "npm", ["npm", "/usr/bin/npm", "/usr/local/bin/npm"],
        proc_pattern="-x npm"
    )


def detect_php(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, count = _run_check(args, "pgrep -cf 'php-fpm: pool' 2>/dev/null || pgrep -cf php-fpm 2>/dev/null || echo 0")
        if rc == 0 and count.strip().isdigit():
            s.details["pool_children"] = count.strip()
        rc, ext_count = _run_check(args, "php -m 2>/dev/null | tail -n +2 | wc -l | tr -d ' '")
        if rc == 0 and ext_count.strip().isdigit():
            s.details["extensions"] = ext_count.strip()

    return _detect_service(
        ssh_args, "PHP", ["php", "/usr/bin/php", "/usr/local/bin/php", "/opt/php/bin/php"],
        proc_pattern="-x php-fpm >/dev/null 2>&1 || pgrep -f 'php-fpm: master'",
        ver_cmd="php -v 2>/dev/null | head -1",
        ver_parser=lambda out: out.split()[1] if len(out.split()) >= 2 else "",
        extra_fn=_extra
    )


def detect_apache(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, modules = _run_check(args, f"{s.path} -M 2>/dev/null | wc -l | tr -d ' '")
        if rc == 0 and modules.strip().isdigit():
            s.details["modules"] = modules.strip()

    return _detect_service(
        ssh_args, "Apache", ["apache2", "httpd", "/usr/sbin/apache2", "/usr/sbin/httpd", "/usr/local/sbin/httpd"],
        proc_pattern="-x apache2 >/dev/null 2>&1 || pgrep -x httpd",
        ver_cmd="apache2 -v 2>/dev/null || httpd -v 2>/dev/null",
        ver_parser=lambda out: (re.search(r"Apache/([\d.]+)", out).group(1) if re.search(r"Apache/([\d.]+)", out) else ""),
        extra_fn=_extra
    )


def detect_nginx(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, sites = _run_check(args, "ls -1 /etc/nginx/sites-enabled/ 2>/dev/null | wc -l | tr -d ' ' || echo 0")
        if rc == 0 and sites.strip().isdigit():
            s.details["sites_enabled"] = sites.strip()
        rc, procs = _run_check(args, "pgrep -cf 'nginx: worker' 2>/dev/null || echo 0")
        if rc == 0 and procs.strip().isdigit():
            s.details["worker_processes"] = procs.strip()

    return _detect_service(
        ssh_args, "Nginx", ["nginx", "/usr/sbin/nginx", "/usr/local/sbin/nginx", "/opt/nginx/sbin/nginx"],
        proc_pattern="-x nginx",
        ver_cmd="nginx -v 2>&1",
        ver_parser=lambda out: (re.search(r"nginx/([\d.]+)", out).group(1) if re.search(r"nginx/([\d.]+)", out) else ""),
        extra_fn=_extra
    )


def detect_caddy(ssh_args: list[str]) -> ServiceStatus:
    def _extra(args: list[str], s: ServiceStatus) -> None:
        rc, procs = _run_check(args, "pgrep -cf caddy 2>/dev/null || echo 0")
        if rc == 0 and procs.strip().isdigit():
            s.details["processes"] = procs.strip()

    def _caddy_ver(out: str) -> str:
        m = re.search(r"v?(\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
        return out.split()[0].lstrip("v") if out.split() else out[:30]

    return _detect_service(
        ssh_args, "Caddy", ["caddy", "/usr/bin/caddy", "/usr/local/bin/caddy", "/opt/caddy/caddy"],
        proc_pattern="-x caddy >/dev/null 2>&1 || pgrep -f 'caddy run'",
        ver_cmd="caddy version 2>&1",
        ver_parser=_caddy_ver,
        extra_fn=_extra
    )


def detect_all_services(host: str, user: str = "", port: int = 22, key: str = "") -> list[ServiceStatus]:
    """Run all service detectors and return results.

    Raises ConnectionError if the host cannot be reached, instead of running
    every detector into its own timeout and reporting each service missing.
    """
    ssh_args = build_ssh_args(host=host, user=user, port=port, key=key)
    rc, out = _run_check(ssh_args, "true", timeout=15)
    if rc != 0:
        raise ConnectionError(out or f"ssh exited with code {rc}")
    return [
        detect_redis(ssh_args),
        detect_supervisor(ssh_args),
        detect_php_fpm(ssh_args),
        detect_php(ssh_args),
        detect_apache(ssh_args),
        detect_nginx(ssh_args),
        detect_caddy(ssh_args),
        detect_nodejs(ssh_args),
        detect_npm(ssh_args),
    ]
