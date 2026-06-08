"""Remote service detection for spyro doctor.

Detects: Redis, Supervisor, PHP-FPM, Node.js/npm on remote servers.
Each detector runs a lightweight SSH check and returns structured results.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Optional

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


def _run_check(ssh_args: list[str], cmd: str, timeout: int = 3) -> tuple[int, str]:
    """Run a command on the remote server via SSH.

    Args:
        ssh_args: Base SSH arguments
        cmd: Command to run on remote
        timeout: Seconds to wait before killing (default 3)

    Returns:
        Tuple of (return_code, stdout_string)
    """
    full_cmd = ssh_args + [cmd]
    try:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.decode().strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)


def _get_version(ssh_args: list[str], cmd: str) -> str:
    """Extract version string from a remote command."""
    rc, output = _run_check(ssh_args, cmd)
    if rc == 0 and output:
        # Take first line, extract version-like string
        line = output.split("\n")[0].strip()
        # Common patterns: "redis-cli 7.0.0", "v18.0.0", "1.22.3"
        for word in line.split():
            if any(c.isdigit() for c in word):
                # Strip leading v/V
                ver = word.lstrip("vV")
                if "." in ver:
                    return ver
        return line[:50]
    return ""


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


def detect_redis(ssh_args: list[str]) -> ServiceStatus:
    """Detect Redis server on remote host."""
    status = ServiceStatus(name="Redis")

    # Find redis-server binary (single SSH call)
    rc, path = _run_check(ssh_args, "command -v redis-server 2>/dev/null || echo ''")
    if rc == 0 and path:
        status.available = True
        status.path = path
    else:
        # Try common locations with a single test call
        rc, path = _run_check(ssh_args, "for p in /usr/bin/redis-server /usr/local/bin/redis-server /opt/redis/bin/redis-server; do test -x \"$p\" && echo \"$p\" && break; done")
        if rc == 0 and path:
            status.available = True
            status.path = path

    if not status.available:
        status.error = "redis-server not found"
        return status

    # Get version
    status.version = _get_version(ssh_args, f"{status.path} --version 2>/dev/null")

    # Check if running
    rc, _ = _run_check(ssh_args, "pgrep -x redis-server >/dev/null 2>&1")
    status.running = rc == 0

    # Get redis-cli info if running
    if status.running:
        rc, info = _run_check(ssh_args, "timeout 5 redis-cli info server 2>/dev/null | grep -E 'redis_version|tcp_port|os'", timeout=5)
        if rc == 0 and info:
            for line in info.split("\n"):
                if ":" in line:
                    key, _, val = line.partition(":")
                    val = val.strip()
                    if key.strip() == "redis_version":
                        status.version = val
                    elif key.strip() == "tcp_port":
                        status.details["port"] = val
                    elif key.strip() == "os":
                        status.details["os"] = val

    return status


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def detect_supervisor(ssh_args: list[str]) -> ServiceStatus:
    """Detect Supervisor (supervisord/supervisorctl) on remote host."""
    status = ServiceStatus(name="Supervisor")

    # Find supervisorctl (single SSH call)
    rc, path = _run_check(ssh_args, "command -v supervisorctl 2>/dev/null || for p in /usr/bin/supervisorctl /usr/local/bin/supervisorctl; do test -x \"$p\" && echo \"$p\" && break; done")
    if rc == 0 and path:
        status.available = True
        status.path = path

    if not status.available:
        status.error = "supervisorctl not found"
        return status

    # Get version
    status.version = _get_version(ssh_args, f"{status.path} --version 2>/dev/null")

    # Check if supervisord is running
    rc, _ = _run_check(ssh_args, "pgrep -x supervisord >/dev/null 2>&1 || pgrep -f 'python.*supervisord' >/dev/null 2>&1")
    status.running = rc == 0

    # Get managed processes if running
    if status.running:
        rc, procs = _run_check(ssh_args, f"{status.path} status 2>/dev/null | head -20", timeout=5)
        if rc == 0 and procs:
            running_count = procs.lower().count("running")
            stopped_count = procs.lower().count("stopped")
            status.details["running_processes"] = str(running_count)
            status.details["stopped_processes"] = str(stopped_count)

    return status


# ---------------------------------------------------------------------------
# PHP-FPM
# ---------------------------------------------------------------------------


def detect_php_fpm(ssh_args: list[str]) -> ServiceStatus:
    """Detect PHP-FPM on remote host."""
    status = ServiceStatus(name="PHP-FPM")

    # Find php-fpm binary (single SSH call for all candidates)
    rc, path = _run_check(ssh_args, "for c in php-fpm php-fpm8.3 php-fpm8.2 php-fpm8.1 php-fpm8.0 php-fpm7.4 php8.3-fpm php8.2-fpm php8.1-fpm php8.0-fpm php7.4-fpm; do command -v \"$c\" 2>/dev/null && break; done")
    if rc == 0 and path:
        status.available = True
        status.path = path

    if not status.available:
        # Try to find via php -i
        rc, php_path = _run_check(ssh_args, "which php 2>/dev/null || echo ''")
        if rc == 0 and php_path:
            status.available = True
            status.path = php_path
            status.error = "php-fpm binary not found, but PHP is available"
        else:
            status.error = "PHP-FPM not found"
            return status

    # Get PHP version
    rc, php_ver = _run_check(ssh_args, "php -v 2>/dev/null | head -1")
    if rc == 0 and php_ver:
        # Extract "PHP 8.1.2" -> "8.1.2"
        parts = php_ver.split()
        if len(parts) >= 2:
            status.version = parts[1]

    # Check if php-fpm process is running
    rc, _ = _run_check(ssh_args, "pgrep -f 'php-fpm.*master' >/dev/null 2>&1 || pgrep -x php-fpm >/dev/null 2>&1")
    status.running = rc == 0

    # Get pool info if running
    if status.running:
        rc, pools = _run_check(ssh_args, "php-fpm -tt 2>/dev/null | grep '\\[pool' | wc -l || echo 0", timeout=5)
        if rc == 0 and pools.strip().isdigit():
            status.details["pools"] = pools.strip()

    return status


# ---------------------------------------------------------------------------
# Node.js / npm
# ---------------------------------------------------------------------------


def detect_nodejs(ssh_args: list[str]) -> ServiceStatus:
    """Detect Node.js on remote host."""
    status = ServiceStatus(name="Node.js")

    # Find node binary (single SSH call)
    rc, path = _run_check(ssh_args, "command -v node 2>/dev/null || for p in /usr/bin/node /usr/local/bin/node /opt/node/bin/node; do test -x \"$p\" 2>/dev/null && echo \"$p\" && break; done")
    if rc == 0 and path:
        status.available = True
        status.path = path

    if not status.available:
        status.error = "node not found"
        return status

    # Get version
    status.version = _get_version(ssh_args, f"{status.path} --version 2>/dev/null")

    # Check if any node processes are running
    rc, _ = _run_check(ssh_args, "pgrep -x node >/dev/null 2>&1 || pgrep -f 'node ' >/dev/null 2>&1")
    status.running = rc == 0

    return status


def detect_npm(ssh_args: list[str]) -> ServiceStatus:
    """Detect npm on remote host."""
    status = ServiceStatus(name="npm")

    # Find npm binary (single SSH call)
    rc, path = _run_check(ssh_args, "command -v npm 2>/dev/null || for p in /usr/bin/npm /usr/local/bin/npm; do test -x \"$p\" && echo \"$p\" && break; done")
    if rc == 0 and path:
        status.available = True
        status.path = path

    if not status.available:
        status.error = "npm not found"
        return status

    # Get version
    status.version = _get_version(ssh_args, f"{status.path} --version 2>/dev/null")

    return status


# ---------------------------------------------------------------------------
# PHP
# ---------------------------------------------------------------------------


def detect_php(ssh_args: list[str]) -> ServiceStatus:
    """Detect PHP CLI on remote host."""
    status = ServiceStatus(name="PHP")

    rc, path = _run_check(ssh_args, "command -v php 2>/dev/null || echo ''")
    if rc == 0 and path:
        status.available = True
        status.path = path
    else:
        for loc in ["/usr/bin/php", "/usr/local/bin/php", "/opt/php/bin/php"]:
            rc, _ = _run_check(ssh_args, f"test -x {loc} 2>/dev/null && echo ok")
            if rc == 0:
                status.available = True
                status.path = loc
                break

    if not status.available:
        status.error = "php not found"
        return status

    rc, ver = _run_check(ssh_args, "php -v 2>/dev/null | head -1")
    if rc == 0 and ver:
        parts = ver.split()
        if len(parts) >= 2:
            status.version = parts[1]  # "PHP 8.4.5" -> "8.4.5"

    rc, _ = _run_check(ssh_args, "pgrep -x php-fpm >/dev/null 2>&1 || pgrep -f 'php-fpm: master' >/dev/null 2>&1")
    status.running = rc == 0

    if status.running:
        rc, count = _run_check(ssh_args, "pgrep -cf 'php-fpm: pool' 2>/dev/null || pgrep -cf php-fpm 2>/dev/null || echo 0")
        if rc == 0 and count.strip().isdigit():
            status.details["pool_children"] = count.strip()

    # Count loaded extensions
    rc, ext_count = _run_check(ssh_args, "php -m 2>/dev/null | tail -n +2 | wc -l | tr -d ' '")
    if rc == 0 and ext_count.strip().isdigit():
        status.details["extensions"] = ext_count.strip()

    return status


# ---------------------------------------------------------------------------
# Apache
# ---------------------------------------------------------------------------


def detect_apache(ssh_args: list[str]) -> ServiceStatus:
    """Detect Apache httpd on remote host."""
    status = ServiceStatus(name="Apache")

    # Try both apache2 and httpd
    rc, path = _run_check(ssh_args, "command -v apache2 2>/dev/null || command -v httpd 2>/dev/null || echo ''")
    if rc == 0 and path:
        status.available = True
        status.path = path
    else:
        for loc in ["/usr/sbin/apache2", "/usr/sbin/httpd", "/usr/local/sbin/httpd"]:
            rc, _ = _run_check(ssh_args, f"test -x {loc} 2>/dev/null && echo ok")
            if rc == 0:
                status.available = True
                status.path = loc
                break

    if not status.available:
        status.error = "apache2/httpd not found"
        return status

    rc, ver = _run_check(ssh_args, f"{status.path} -v 2>/dev/null | head -1")
    if rc == 0 and ver:
        # "Server version: Apache/2.4.62 (Ubuntu)" -> "2.4.62"
        import re
        m = re.search(r"Apache/([\d.]+)", ver)
        if m:
            status.version = m.group(1)

    rc, _ = _run_check(ssh_args, "pgrep -x apache2 >/dev/null 2>&1 || pgrep -x httpd >/dev/null 2>&1")
    status.running = rc == 0

    if status.running:
        rc, modules = _run_check(ssh_args, f"{status.path} -M 2>/dev/null | wc -l | tr -d ' '")
        if rc == 0 and modules.strip().isdigit():
            status.details["modules"] = modules.strip()

    return status


# ---------------------------------------------------------------------------
# Nginx
# ---------------------------------------------------------------------------


def detect_nginx(ssh_args: list[str]) -> ServiceStatus:
    """Detect Nginx on remote host."""
    status = ServiceStatus(name="Nginx")

    rc, path = _run_check(ssh_args, "command -v nginx 2>/dev/null || echo ''")
    if rc == 0 and path:
        status.available = True
        status.path = path
    else:
        for loc in ["/usr/sbin/nginx", "/usr/local/sbin/nginx", "/opt/nginx/sbin/nginx"]:
            rc, _ = _run_check(ssh_args, f"test -x {loc} 2>/dev/null && echo ok")
            if rc == 0:
                status.available = True
                status.path = loc
                break

    if not status.available:
        status.error = "nginx not found"
        return status

    rc, ver = _run_check(ssh_args, f"{status.path} -v 2>&1")
    if rc == 0 and ver:
        # "nginx version: nginx/1.26.3" -> "1.26.3"
        import re
        m = re.search(r"nginx/([\d.]+)", ver)
        if m:
            status.version = m.group(1)

    rc, _ = _run_check(ssh_args, "pgrep -x nginx >/dev/null 2>&1")
    status.running = rc == 0

    if status.running:
        rc, sites = _run_check(ssh_args, "ls -1 /etc/nginx/sites-enabled/ 2>/dev/null | wc -l | tr -d ' ' || echo 0")
        if rc == 0 and sites.strip().isdigit():
            status.details["sites_enabled"] = sites.strip()
        rc, procs = _run_check(ssh_args, "pgrep -cf 'nginx: worker' 2>/dev/null || echo 0")
        if rc == 0 and procs.strip().isdigit():
            status.details["worker_processes"] = procs.strip()

    return status


# ---------------------------------------------------------------------------
# Caddy
# ---------------------------------------------------------------------------


def detect_caddy(ssh_args: list[str]) -> ServiceStatus:
    """Detect Caddy web server on remote host."""
    status = ServiceStatus(name="Caddy")

    rc, path = _run_check(ssh_args, "command -v caddy 2>/dev/null || echo ''")
    if rc == 0 and path:
        status.available = True
        status.path = path
    else:
        for loc in ["/usr/bin/caddy", "/usr/local/bin/caddy", "/opt/caddy/caddy"]:
            rc, _ = _run_check(ssh_args, f"test -x {loc} 2>/dev/null && echo ok")
            if rc == 0:
                status.available = True
                status.path = loc
                break

    if not status.available:
        status.error = "caddy not found"
        return status

    rc, ver = _run_check(ssh_args, f"{status.path} version 2>&1")
    if rc == 0 and ver:
        # "v2.9.1 h1:hash..." -> "2.9.1"
        import re
        m = re.search(r"v?(\d+\.\d+\.\d+)", ver)
        if m:
            status.version = m.group(1)
        else:
            status.version = ver.split()[0].lstrip("v") if ver.split() else ver[:30]

    rc, _ = _run_check(ssh_args, "pgrep -x caddy >/dev/null 2>&1 || pgrep -f 'caddy run' >/dev/null 2>&1")
    status.running = rc == 0

    if status.running:
        rc, procs = _run_check(ssh_args, "pgrep -cf caddy 2>/dev/null || echo 0")
        if rc == 0 and procs.strip().isdigit():
            status.details["processes"] = procs.strip()

    return status


# ---------------------------------------------------------------------------
# Aggregate detector
# ---------------------------------------------------------------------------


def detect_all_services(host: str, user: str = "", port: int = 22, key: str = "") -> list[ServiceStatus]:
    """Run all service detectors and return results.

    Each service check has a per-command timeout of 5s, controlled by _run_check().
    """
    ssh_args = build_ssh_args(host=host, user=user, port=port, key=key)

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
