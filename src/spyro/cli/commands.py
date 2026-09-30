"""CLI command implementations for spyro."""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from ..utils.config import (
    DatabaseConfig,
    ProfileConfig,
    SpyroConfig,
    generate_config,
    load_config,
    resolve_profile,
)
from ..core.db import (
    client_argv,
    client_env,
    fetch_remote_file,
    generate_connection_url,
    resolve_db_credentials,
)
from ..core.services import detect_all_services
from ..core.sync import (
    SyncPin, load_pins, add_pin, remove_pin,
    detect_framework, should_exclude, filter_files,
    FRAMEWORK_EXCLUSIONS, SENSITIVE_PATTERNS,
)
from ..core.pty_engine import PTYRunner, build_scp_args, build_ssh_args
from ..supervisor.state import (
    all_tunnels,
    get_tunnel,
    tunnel_alive,
)
from ..supervisor.tunnel import TunnelManager, db_local_port
from ..utils.paths import safe_quote

console = Console()
err_console = Console(stderr=True)
log = logging.getLogger("spyro")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _remote(line: str, prefix: str = "") -> None:
    """Print a line of remote output verbatim.

    Remote text must never be parsed as Rich markup (``[/var/www]`` raises,
    ``[stacktrace]`` silently vanishes) or emoji codes, nor re-wrapped.
    """
    console.print(Text(prefix + line), soft_wrap=True)


def _emit_json(obj: object) -> None:
    """Machine-readable output: plain stdout, never wrapped or highlighted."""
    click.echo(json.dumps(obj, indent=2, default=str))


def _report_exit(ec: int, timeout: float | None, prefix: str = "  ") -> None:
    if ec == 124:
        console.print(
            f"{prefix}[red]Timed out after {timeout:g}s. The remote command may still be "
            f"running; raise the limit with --timeout.[/red]"
        )
    elif ec != 0:
        console.print(f"{prefix}[red]Exit code: {ec}[/red]")


def _sudo_prefix(p: "ProfileConfig", no_escalate: bool = False) -> str:
    """``sudo `` (or ``sudo -u <sudo_user> ``) for app commands, ``""`` when not escalating.

    Running the app as root leaves root-owned files in storage/ and cache/ that
    the web server user can no longer write; ``sudo_user`` avoids that.
    """
    if no_escalate or not p.sudo:
        return ""
    return f"sudo -u {safe_quote(p.sudo_user)} " if p.sudo_user else "sudo "


def _tunnel_for(config: SpyroConfig, profile_name: str):
    """A live tunnel for the profile, starting one if needed (exits with the ssh error on failure)."""
    state = get_tunnel(profile_name)
    if not (state and state.status == "running" and tunnel_alive(state)):
        console.print(f"[cyan]Starting tunnel for {profile_name}...[/cyan]")
    try:
        return TunnelManager(config).ensure(profile_name)
    except RuntimeError as e:
        raise click.ClickException(f"Could not start tunnel for '{profile_name}': {e}") from None


def _resolve_db(p: "ProfileConfig", profile_name: str) -> DatabaseConfig:
    """``[profiles.x.db]`` as configured, or read from the remote .env when its password is empty."""
    if p.db.password:
        return p.db
    from ..utils.keychain import prompt_for_credential

    console.print("[dim]db.password is empty: reading credentials from the remote .env...[/dim]")
    return resolve_db_credentials(p, password=prompt_for_credential(profile_name, p.user))


def _db_target(profile_name: str, *, no_tunnel: bool = False, port: int | None = None):
    """Everything a local DB client needs: ``(profile, database config, local port)``."""
    config = load_config()
    p = config.get_profile(profile_name)
    db = _resolve_db(p, profile_name)
    if port:
        local_port = port
    elif no_tunnel:
        local_port = db.port
    else:
        local_port = db_local_port(p, _tunnel_for(config, profile_name))
    return p, db, local_port


# ---------------------------------------------------------------------------
# spyro init
# ---------------------------------------------------------------------------


@click.command()
@click.option("--skip-deps", is_flag=True, help="Skip dependency audit")
def cmd_init(skip_deps: bool) -> None:
    """Bootstrap configuration and run toolchain audit."""
    console.print("[bold cyan]Spyro Init[/bold cyan]")

    from ..utils.paths import discover_config

    existing = discover_config()
    if existing:
        console.print(f"[yellow]Configuration already exists at {existing}[/yellow]")
        return

    path = generate_config()
    console.print(f"[green]Created {path}[/green]")

    if not skip_deps:
        console.print("\n[bold]Toolchain audit:[/bold]")
        _audit_deps()


def _audit_deps() -> None:
    """Check that required system tools are available."""
    tools = ["ssh", "scp", "ssh-keygen"]
    for tool in tools:
        found = shutil.which(tool)
        if found:
            console.print(f"  [green]✓[/green] {tool}: {found}")
        else:
            console.print(f"  [red]✗[/red] {tool}: not found")


# ---------------------------------------------------------------------------
# spyro up
# ---------------------------------------------------------------------------


@click.command()
@click.argument("profile", required=False)
@click.option("--no-daemon", is_flag=True, help="Run in the foreground (Ctrl+C stops it)")
def cmd_up(profile: str | None, no_daemon: bool) -> None:
    """Start tunnels for a profile (or every profile with forwarded_ports)."""
    config = load_config()
    manager = TunnelManager(config)

    if profile:
        profiles = [profile]
    else:
        profiles = [n for n in config.profile_names if config.profiles[n].forwarded_ports]
        if not profiles:
            console.print("[yellow]No profile has forwarded_ports configured[/yellow]")
            return

    failed = 0
    for name in profiles:
        console.print(f"[cyan]Starting tunnel: {name}[/cyan]")
        try:
            state = manager.start(
                name,
                foreground=no_daemon,
                on_ready=lambda st, n=name: _print_tunnel_info(n, st),
            )
            if not no_daemon:
                _print_tunnel_info(name, state)
        except (RuntimeError, SystemExit) as e:
            failed += 1
            console.print(Text(f"Failed to start '{name}': {e}", style="red"))
    if failed:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# spyro down
# ---------------------------------------------------------------------------


@click.command()
@click.argument("profile", required=False)
def cmd_down(profile: str | None) -> None:
    """Stop tunnels for a profile (or all if omitted)."""
    config = load_config()
    manager = TunnelManager(config)

    if profile:
        if manager.stop(profile):
            console.print(f"[green]Stopped tunnel: {profile}[/green]")
        else:
            console.print(f"[yellow]No active tunnel for '{profile}'[/yellow]")
    else:
        count = manager.stop_all()
        console.print(f"[green]Stopped {count} tunnel(s)[/green]")


# ---------------------------------------------------------------------------
# spyro status
# ---------------------------------------------------------------------------


@click.command()
@click.argument("profile", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def cmd_status(profile: str | None, json_output: bool) -> None:
    """Display health, active tunnels, and port mappings."""
    config = load_config()
    manager = TunnelManager(config)
    statuses = manager.status(profile)

    if not statuses:
        if json_output:
            _emit_json({"tunnels": [], "status": "no_tunnels"})
        else:
            console.print("[yellow]No tunnels configured[/yellow]")
        return

    if json_output:
        _emit_json({"tunnels": statuses})
        return

    table = Table(title="Spyro Tunnels")
    table.add_column("Profile", style="cyan")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Ports")
    table.add_column("Started")

    for name, info in statuses.items():
        status_style = {
            "running": "green",
            "stopped": "red",
            "stale": "yellow",
        }.get(info["status"], "dim")

        ports = ", ".join(str(p) for p in info["forwarded_ports"]) or "—"

        table.add_row(
            name,
            f"[{status_style}]{info['status']}[/{status_style}]",
            str(info["pid"]) or "—",
            ports,
            info["started_at"][:19] if info["started_at"] else "—",
        )

    console.print(table)

def _show_log(path: Path, follow: bool) -> None:
    """Display a log file, optionally following."""
    if follow:
        try:
            subprocess.run(["tail", "-f", str(path)])
        except KeyboardInterrupt:
            pass
    else:
        try:
            lines = path.read_text().splitlines()
            for line in lines[-50:]:
                _remote(line)
        except FileNotFoundError:
            console.print(f"[red]Log file not found: {path}[/red]")


# ---------------------------------------------------------------------------
# Capistrano deployment detection


def _get_timeout(config: "SpyroConfig", cli_timeout: float | None, default: float) -> float:
    """Resolve timeout: CLI flag -> [defaults] command_timeout -> default.

    Raises click.BadParameter for invalid values.
    """
    if cli_timeout is not None:
        if cli_timeout <= 0:
            raise click.BadParameter("--timeout must be > 0")
        return cli_timeout
    defaults = config.global_settings.get("defaults", {})
    if isinstance(defaults, dict):
        ct = defaults.get("command_timeout")
        if ct is not None:
            if isinstance(ct, (int, float)) and ct > 0:
                return float(ct)
            raise click.BadParameter(
                f"[defaults] command_timeout must be a positive number, got {ct!r}"
            )
    return default


def _capistrano_cd(remote_path: str) -> str:
    """Build a ``cd`` fragment that enters the Capistrano ``current`` symlink if present.

    Returns a shell fragment like::

        cd /var/www/app && [ -L current ] && cd current

    Falls back to just ``cd /var/www/app`` when no symlink exists.
    """
    return (
        f"cd {safe_quote(remote_path)}"
        f" && [ -L current ] && cd current || cd {safe_quote(remote_path)}"
    )


# ---------------------------------------------------------------------------
# WordPress detection helpers
# ---------------------------------------------------------------------------


def _detect_wordpress(ssh_args: list[str], remote_path: str) -> dict[str, bool]:
    """Detect WordPress installation on remote server.

    Checks for wp-config.php, wp-content/, wp-includes/, and WP-CLI.
    """
    indicators = {
        "wp_config": False,
        "wp_content": False,
        "wp_includes": False,
        "wp_cli": False,
    }

    checks = [
        ("wp_config", f"test -f {remote_path}/wp-config.php"),
        ("wp_content", f"test -d {remote_path}/wp-content"),
        ("wp_includes", f"test -d {remote_path}/wp-includes"),
        ("wp_cli", "which wp || test -f /usr/local/bin/wp || test -f /usr/bin/wp"),
    ]

    for key, check_cmd in checks:
        cmd = ssh_args + [check_cmd]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            indicators[key] = result.returncode == 0
        except Exception:
            pass

    return indicators


def _find_wp_cli(ssh_args: list[str], wp_cli_path: str = "") -> str:
    """Find WP-CLI on the remote server."""
    if wp_cli_path:
        return wp_cli_path

    candidates = ["wp", "/usr/local/bin/wp", "/usr/bin/wp", "~/bin/wp", "wp-cli.phar"]

    for candidate in candidates:
        cmd = ssh_args + [f"which {candidate} 2>/dev/null || test -x {candidate}"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0:
                output = result.stdout.decode().strip()
                return output if output else candidate
        except Exception:
            pass

    return "wp"


# ---------------------------------------------------------------------------
# spyro doctor
# ---------------------------------------------------------------------------


@click.command()
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def cmd_doctor(json_output: bool) -> None:
    """Run automated diagnostics."""
    issues: list[str] = []
    results: dict[str, list[dict]] = {}

    def _record(section: str, check: str, ok: bool, detail: str = "") -> None:
        results.setdefault(section, []).append({
            "check": check,
            "ok": ok,
            "detail": detail,
        })

    if not json_output:
        console.print("[bold cyan]Spyro Doctor[/bold cyan]\n")

    # 1. SSH connectivity
    if not json_output:
        console.print("[bold]1. SSH connectivity[/bold]")
    config = load_config()
    for name, profile in config.profiles.items():
        ssh_args = build_ssh_args(
            host=profile.host,
            user=profile.user,
            port=profile.port,
            key=profile.key,
        )
        ssh_args.extend(["-o", "ConnectTimeout=5", "echo", "spyro-ok"])

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and "spyro-ok" in result.stdout.decode():
                if not json_output:
                    console.print(f"  [green]✓[/green] {name}: reachable")
                _record("ssh_connectivity", name, True)
            else:
                if not json_output:
                    console.print(f"  [red]✗[/red] {name}: connection failed")
                issues.append(f"SSH to {name} failed")
                _record("ssh_connectivity", name, False, "connection failed")
        except subprocess.TimeoutExpired:
            if not json_output:
                console.print(f"  [yellow]⚠[/yellow] {name}: timeout")
            issues.append(f"SSH to {name} timed out")
            _record("ssh_connectivity", name, False, "timeout")
        except FileNotFoundError:
            if not json_output:
                console.print(f"  [red]✗[/red] ssh not found")
            issues.append("ssh binary not found")
            _record("ssh_connectivity", "ssh_binary", False, "not found")
            break

    # 2. Remote path validity
    if not json_output:
        console.print("\n[bold]2. Remote path validity[/bold]")
    for name, profile in config.profiles.items():
        ssh_args = build_ssh_args(
            host=profile.host,
            user=profile.user,
            port=profile.port,
            key=profile.key,
        )
        ssh_args.extend(["test", "-d", profile.remote_path])

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                if not json_output:
                    console.print(f"  [green]✓[/green] {name}: {profile.remote_path} exists")
                _record("remote_path", name, True)
            else:
                if not json_output:
                    console.print(f"  [yellow]⚠[/yellow] {name}: {profile.remote_path} not found")
                issues.append(f"Remote path missing on {name}")
                _record("remote_path", name, False, "not found")
        except Exception:
            if not json_output:
                console.print(f"  [yellow]⚠[/yellow] {name}: could not verify")
            _record("remote_path", name, False, "could not verify")

    # 2.5 Deployment structure check (Capistrano symlink awareness)
    artisan_names = [n for n, p in config.profiles.items() if p.artisan]
    if artisan_names:
        if not json_output:
            console.print("\n[bold]   Deployment structure[/bold]")
        for name in artisan_names:
            p = config.profiles[name]
            ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
            # Check if remote_path/current is a symlink
            check_cmd = f"test -L {safe_quote(p.remote_path)}/current"
            ssh_args_copy = list(ssh_args) + [check_cmd]
            try:
                result = subprocess.run(ssh_args_copy, capture_output=True, timeout=10)
                if result.returncode == 0:
                    if not json_output:
                        console.print(f"  [green]✓[/green] {name}: Capistrano 'current' symlink detected")
                    _record("deploy_structure", name, True, "capistrano symlink")
                else:
                    if not json_output:
                        console.print(f"  [dim]  ℹ[/dim] {name}: plain docroot (no 'current' symlink)")
                    _record("deploy_structure", name, False, "plain docroot")
            except Exception:
                _record("deploy_structure", name, False, "could not verify")

    # 3. Local port conflicts
    if not json_output:
        console.print("\n[bold]3. Local port conflicts[/bold]")
    from ..supervisor.tunnel import _port_available

    for name, profile in config.profiles.items():
        for port in profile.forwarded_ports:
            available = _port_available(port)
            if not json_output:
                if available:
                    console.print(f"  [green]✓[/green] Port {port}: available")
                else:
                    console.print(f"  [yellow]⚠[/yellow] Port {port}: in use")
                    issues.append(f"Port {port} conflict for {name}")
            _record("port_conflicts", f"{name}:{port}", available)

    # 4. Laravel artisan detection
    if not json_output:
        console.print("\n[bold]4. Laravel artisan detection[/bold]")
    for name, profile in config.profiles.items():
        if not profile.artisan:
            continue
        ssh_args = build_ssh_args(
            host=profile.host,
            user=profile.user,
            port=profile.port,
            key=profile.key,
        )
        check_cmd = (
            f"cd {safe_quote(profile.remote_path)}"
            f" && ([ -L current ] && cd current)"
            f" && test -f artisan"
        )
        ssh_args.extend([check_cmd])

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                if not json_output:
                    console.print(f"  [green]✓[/green] {name}: artisan found")
                _record("artisan", name, True)
            else:
                if not json_output:
                    console.print(f"  [red]✗[/red] {name}: artisan not found")
                issues.append(f"Artisan not found on {name}")
                _record("artisan", name, False)
        except Exception:
            if not json_output:
                console.print(f"  [yellow]⚠[/yellow] {name}: could not verify")
            _record("artisan", name, False, "could not verify")

    # 5. WordPress detection
    wp_profiles = [n for n, p in config.profiles.items() if p.wordpress]
    if wp_profiles:
        if not json_output:
            console.print("\n[bold]5. WordPress detection[/bold]")
        for name in wp_profiles:
            profile = config.profiles[name]
            ssh_args = build_ssh_args(
                host=profile.host,
                user=profile.user,
                port=profile.port,
                key=profile.key,
            )
            indicators = _detect_wordpress(ssh_args, profile.remote_path)

            wp_ok = indicators["wp_config"]
            if not json_output:
                if wp_ok:
                    console.print(f"  [green]✓[/green] {name}: WordPress detected")
                    if indicators["wp_cli"]:
                        console.print(f"    [green]✓[/green] WP-CLI available")
                    else:
                        console.print(f"    [yellow]⚠[/yellow] WP-CLI not found")
                        issues.append(f"WP-CLI not found on {name}")
                else:
                    console.print(f"  [yellow]⚠[/yellow] {name}: WordPress not detected")
                    issues.append(f"WordPress not detected on {name} (wordpress=true in config)")
            _record("wordpress", name, wp_ok, str(indicators) if json_output else "")

    # 6. Remote service detection
    if not json_output:
        console.print("\n[bold]6. Remote services[/bold]")
    for name, profile in config.profiles.items():
        if not json_output:
            console.print(f"\n  [cyan]{name}[/cyan] ({profile.host})")
        try:
            services = detect_all_services(
                host=profile.host,
                user=profile.user,
                port=profile.port,
                key=profile.key,
            )
            for svc in services:
                if not json_output:
                    # summary/path/details come from the remote host: never markup
                    line = f"    {svc.icon} {escape(svc.summary)}"
                    if svc.path:
                        line += f" ({escape(svc.path)})"
                    console.print(line)
                    if svc.details:
                        for k, v in svc.details.items():
                            console.print(f"      {escape(k)}: {escape(v)}")
                _record("remote_services", f"{name}/{svc.summary}", True, str(svc.details) if json_output else "")
        except Exception as e:
            if not json_output:
                console.print(f"    [yellow]⚠ Service check interrupted: {escape(str(e))}[/yellow]")
            issues.append(f"Service check failed for {name}: {e}")
            _record("remote_services", name, False, str(e))

    if json_output:
        _emit_json({
            "sections": results,
            "issues": issues,
            "healthy": len(issues) == 0,
        })
        return

    console.print(f"\n[bold]Summary:[/bold] {len(issues)} issue(s) found")
    if issues:
        for issue in issues:
            console.print(f"  [red]•[/red] {issue}")
    else:
        console.print("  [green]All checks passed[/green]")


# ---------------------------------------------------------------------------
# spyro pull-env
# ---------------------------------------------------------------------------


@click.command()
@click.option("--dest", default=".env.remote", help="Output file path")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_pull_env(dest: str, profile: str) -> None:
    """Pull remote environment config to a local file."""
    config = load_config()
    p = config.get_profile(profile)

    from ..utils.keychain import prompt_for_credential

    ssh_pw = prompt_for_credential(profile, p.user)
    console.print(f"[cyan]Pulling .env from {p.host}...[/cyan]")

    for env_file in p.env_files:
        content = fetch_remote_file(p, f"{p.remote_path}/{env_file}", password=ssh_pw)
        if content:
            # Secrets: owner-only, even when the file already existed with looser modes
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(dest, 0o600)
            console.print(Text(f"Saved to {dest}", style="green"))
            return

    raise click.ClickException("Failed to pull environment config")


# ---------------------------------------------------------------------------
# spyro run
# ---------------------------------------------------------------------------


@click.command()
@click.option("--all", "run_all", is_flag=True, help="Run across all profiles")
@click.option("--profile", "-p", multiple=True, help="Specific profile(s)")
@click.option("--timeout", type=float, default=None, help="Command timeout in seconds (default: 60)")
@click.option("--chdir", "-C", is_flag=True, help="cd to remote_path before running command")
@click.argument("command")
def cmd_run(run_all: bool, profile: tuple[str, ...], timeout: float | None, chdir: bool, command: str) -> None:
    """Execute a command on remote server(s)."""
    config = load_config()

    if run_all:
        profiles = config.profile_names
    elif profile:
        profiles = list(profile)
    else:
        env_profile = os.environ.get("SPYRO_PROFILE", "")
        if env_profile:
            profiles = [n.strip() for n in env_profile.split(",") if n.strip()]
        else:
            console.print("[red]Specify --all, --profile, or set SPYRO_PROFILE[/red]")
            return

    for name in profiles:
        p = config.get_profile(name)
        console.print(f"\n[bold cyan]=== {name} ===[/bold cyan]")

        # Optionally wrap command with cd to remote_path
        remote_cmd = f"{_capistrano_cd(p.remote_path)} && {command}" if chdir else command
        _run_svc_cmd(name, remote_cmd, timeout=60.0, cli_timeout=timeout, escalate=p.sudo)


# ---------------------------------------------------------------------------
# spyro watch
# ---------------------------------------------------------------------------


@click.command()
@click.argument("src")
@click.argument("dest")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_watch(src: str, dest: str, profile: str) -> None:
    """Sync local file changes to remote server in real-time."""
    src_path = Path(src).resolve()
    if not src_path.exists():
        console.print(f"[red]Source path does not exist: {src}[/red]")
        return
    pin = SyncPin(local_path=str(src_path), remote_path=dest, profile=profile)
    _run_sync_watch(profile, [pin], dry_run=False)

# ---------------------------------------------------------------------------
# spyro proxy-url
# ---------------------------------------------------------------------------


@click.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--port", type=int, help="Override local port")
def cmd_proxy_url(profile: str, port: int | None) -> None:
    """Generate a local connection string for database GUIs."""
    config = load_config()
    p = config.get_profile(profile)
    db = _resolve_db(p, profile)

    tunnel = get_tunnel(profile)
    if port:
        local_port = port
    elif tunnel and tunnel.status == "running" and tunnel_alive(tunnel):
        local_port = db_local_port(p, tunnel)
    else:
        local_port = db.port

    click.echo(generate_connection_url(db, port_override=local_port))


# ---------------------------------------------------------------------------
# spyro artisan
# ---------------------------------------------------------------------------


@click.command(context_settings={"ignore_unknown_options": True})
@click.argument("cmd_args", nargs=-1)
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--timeout", type=float, default=None, help="Command timeout in seconds (default: 60)")
@click.option("--profile", "-p", default=None, help="Profile name (auto-detects if only one exists)")
def cmd_artisan(cmd_args: tuple[str, ...], no_escalate: bool, timeout: float | None, profile: str | None) -> None:
    """Run Laravel Artisan commands on the remote host."""
    profile = resolve_profile(profile)

    if not cmd_args:
        console.print("[red]Usage: spyro artisan <command> [--profile NAME][/red]")
        return

    config = load_config()
    p = config.get_profile(profile)
    if not p.artisan:
        console.print(f"[yellow]Profile '{profile}' is not configured for artisan[/yellow]")
        return

    artisan_cmd = (
        f"{_capistrano_cd(p.remote_path)} && {_sudo_prefix(p, no_escalate)}"
        f"php artisan {' '.join(safe_quote(a) for a in cmd_args)}"
    )
    _run_svc_cmd(
        profile, artisan_cmd, timeout=60.0, cli_timeout=timeout,
        escalate=p.sudo and not no_escalate, prefix="",
    )


# ---------------------------------------------------------------------------
# spyro pin
# ---------------------------------------------------------------------------


@click.command()
@click.argument("local_path")
@click.argument("remote_path")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--framework", "-f", default="auto", help="Framework: auto, laravel, wordpress, node, python, or empty")
@click.option("--exclude", "-e", multiple=True, help="Additional glob patterns to exclude")
def cmd_pin(local_path: str, remote_path: str, profile: str, framework: str, exclude: tuple[str, ...]) -> None:
    """Pin a local directory for automatic sync to remote server."""
    local = Path(local_path).resolve()
    if not local.exists():
        console.print(f"[red]Local path does not exist: {local}[/red]")
        return

    detected = ""
    if framework == "auto":
        detected = detect_framework(local)
        if detected:
            console.print(f"[cyan]Detected framework: {detected}[/cyan]")

    pin = SyncPin(
        local_path=str(local),
        remote_path=remote_path,
        profile=profile,
        framework=detected if framework == "auto" else framework,
        exclude_files=list(exclude),
    )

    exclude_files, exclude_dirs = pin.get_all_excludes()
    console.print(f"[green]Pinned: {local} -> {remote_path}[/green]")
    console.print(f"  Profile: {profile}")
    console.print(f"  Framework: {pin.framework or 'none'}")
    console.print(f"  Excluded files: {len(exclude_files)} patterns")
    console.print(f"  Excluded dirs: {len(exclude_dirs)} patterns")

    add_pin(pin)
    console.print("[green]Pin saved. Use 'spyro sync' to start watching.[/green]")


# ---------------------------------------------------------------------------
# spyro unpin
# ---------------------------------------------------------------------------


@click.command()
@click.argument("local_path")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_unpin(local_path: str, profile: str) -> None:
    """Remove a pinned sync directory."""
    local = str(Path(local_path).resolve())
    remaining = remove_pin(local, profile)
    console.print(f"[green]Removed pin for {local} ({profile})[/green]")
    if remaining:
        console.print(f"  {len(remaining)} pin(s) remaining")


# ---------------------------------------------------------------------------
# spyro pins
# ---------------------------------------------------------------------------


@click.command()
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def cmd_pins(json_output: bool) -> None:
    """List all pinned sync directories."""
    pins = load_pins()
    if not pins:
        if json_output:
            _emit_json({"pins": []})
        else:
            console.print("[yellow]No pinned directories. Use 'spyro pin' to add one.[/yellow]")
        return

    if json_output:
        pin_list = [
            {
                "local": p.local_path,
                "remote": p.remote_path,
                "profile": p.profile,
                "framework": p.framework or "",
            }
            for p in pins
        ]
        _emit_json({"pins": pin_list})
        return

    table = Table(title="Pinned Sync Directories")
    table.add_column("Local", style="cyan")
    table.add_column("Remote")
    table.add_column("Profile")
    table.add_column("Framework")

    for pin in pins:
        table.add_row(pin.local_path, pin.remote_path, pin.profile, pin.framework or "—")

    console.print(table)


# ---------------------------------------------------------------------------
# spyro sync (enhanced watch with exclusions)
# ---------------------------------------------------------------------------


def _run_sync_watch(
    profile: str,
    pins: list[SyncPin],
    dry_run: bool = False,
    stop: "threading.Event | None" = None,
) -> None:
    """Watch directory pins with watchdog and upload changed files.

    Watchdog calls the handler from its own thread, which must not fork PTYs or
    print: it only queues paths. This (main) thread drains the queue.
    """
    import posixpath
    import queue
    import time

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        console.print("[red]watchdog not installed. Run: pip install watchdog[/red]")
        return

    config = load_config()
    p = config.get_profile(profile)

    if not pins:
        console.print(f"[yellow]No pinned directories for profile '{profile}'[/yellow]")
        console.print("Use 'spyro pin <local> <remote> -p <profile>' to add one.")
        return

    console.print(f"[cyan]Syncing {len(pins)} pinned director(y/ies) for {profile}[/cyan]")
    for pin in pins:
        exclude_files, _ = pin.get_all_excludes()
        console.print(Text(f"  {pin.local_path} -> {pin.remote_path} ({len(exclude_files)} exclude patterns)"))

    if dry_run:
        console.print("\n[yellow]Dry run mode -- no files will be uploaded[/yellow]\n")

    changed: "queue.Queue[tuple[Path, SyncPin]]" = queue.Queue()

    class SyncHandler(FileSystemEventHandler):
        """Queue files that were created, modified or moved in.

        Not ``on_any_event``: "opened"/"closed"/"deleted" events would make every
        scp read of a file (an "opened" event on Linux) trigger another upload.
        """

        def _queue(self, path: str, is_directory: bool) -> None:
            if is_directory:
                return
            src = Path(path)
            for pin in pins:
                try:
                    src.relative_to(pin.local_path)
                except ValueError:
                    continue
                changed.put((src, pin))
                return

        def on_created(self, event: object) -> None:
            self._queue(event.src_path, event.is_directory)  # type: ignore[attr-defined]

        def on_modified(self, event: object) -> None:
            self._queue(event.src_path, event.is_directory)  # type: ignore[attr-defined]

        def on_moved(self, event: object) -> None:
            self._queue(event.dest_path, event.is_directory)  # type: ignore[attr-defined]

    observer = Observer()
    for pin in pins:
        local = Path(pin.local_path)
        if local.exists():
            observer.schedule(SyncHandler(), str(local), recursive=True)
            console.print(Text(f"  Watching: {local}", style="dim"))

    from ..core.pty_engine import _scp_target
    from ..utils.keychain import prompt_for_credential

    ssh_pw = "" if dry_run else prompt_for_credential(profile, p.user)
    runner = PTYRunner()
    made_dirs: set[str] = set()

    def upload(src: Path, pin: SyncPin) -> None:
        local_base = Path(pin.local_path)
        exclude_files, exclude_dirs = pin.get_all_excludes()
        rel = src.relative_to(local_base)
        if should_exclude(src, local_base, exclude_files, exclude_dirs, pin.include_patterns):
            if dry_run:
                console.print(Text(f"  Skipped (excluded): {rel}", style="dim"))
            return
        if not src.is_file():  # deleted or replaced before we got to it
            return
        if dry_run:
            console.print(Text(f"  Would sync: {rel}", style="green"))
            return

        remote_file = f"{pin.remote_path.rstrip('/')}/{rel.as_posix()}"
        remote_dir = posixpath.dirname(remote_file)
        if remote_dir not in made_dirs:
            mkdir = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
            mkdir.append(f"mkdir -p {safe_quote(remote_dir)}")
            if runner.run(mkdir, password=ssh_pw, timeout=15.0) != 0:
                console.print(Text(f"  Failed: {rel} (cannot create {remote_dir})", style="red"))
                return
            made_dirs.add(remote_dir)

        scp_args = build_scp_args(
            src=str(src), dest=_scp_target(remote_file, p.host, p.user),
            host=p.host, user=p.user, port=p.port, key=p.key, recursive=False,
        )
        ec = runner.run(scp_args, password=ssh_pw, timeout=60.0)
        if ec == 0:
            console.print(Text(f"  Synced: {rel}", style="green"))
        else:
            console.print(Text(f"  Failed: {rel} (exit code {ec})", style="red"))

    observer.start()
    console.print("\n[cyan]Syncing... (Ctrl+C to stop)[/cyan]\n")
    # Trailing-edge debounce: a file is uploaded once it has been quiet for
    # SETTLE seconds, so a formatter rewriting it right after a save is picked
    # up (a leading-edge debounce would upload the first write and drop the last).
    settle = 0.3
    pending: dict[Path, tuple[SyncPin, float]] = {}
    try:
        while not (stop and stop.is_set()):
            try:
                src, pin = changed.get(timeout=0.1)
                pending[src] = (pin, time.monotonic())
                continue
            except queue.Empty:
                pass
            now = time.monotonic()
            for src in [p_ for p_, (_, t) in pending.items() if now - t >= settle]:
                upload(src, pending.pop(src)[0])
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


@click.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without uploading")
def cmd_sync(profile: str, dry_run: bool) -> None:
    """Watch pinned directories and auto-sync to remote server."""
    all_pins = load_pins()
    pins = [pin for pin in all_pins if pin.profile == profile]
    _run_sync_watch(profile, pins, dry_run=dry_run)

# ---------------------------------------------------------------------------
# spyro wp
# ---------------------------------------------------------------------------


@click.command()
@click.argument("cmd_args", nargs=-1)
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_wp(cmd_args: tuple[str, ...], no_escalate: bool, profile: str) -> None:
    """Run WP-CLI commands on the remote host."""
    if not cmd_args:
        console.print("[red]Usage: spyro wp <command> [--profile NAME][/red]")
        return

    config = load_config()
    p = config.get_profile(profile)

    if not p.wordpress:
        console.print(f"[yellow]Profile '{profile}' is not configured for WordPress[/yellow]")
        return

    # Find WP-CLI on remote
    wp_bin = _find_wp_cli(
        build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key), p.wp_cli_path
    )
    wp_cmd = (
        f"{_capistrano_cd(p.remote_path)} && {_sudo_prefix(p, no_escalate)}"
        f"{wp_bin} {' '.join(safe_quote(a) for a in cmd_args)}"
    )
    _run_svc_cmd(
        profile, wp_cmd, timeout=60.0,
        escalate=p.sudo and not no_escalate, prefix="",
    )


# ---------------------------------------------------------------------------
# spyro cp
# ---------------------------------------------------------------------------


def _is_local_path(path: str) -> bool:
    """Check if a path is local (not a remote scp-style path).

    A path starting with ':' is always treated as remote.
    Everything else is treated as local — absolute paths (``/...``),
    home-relative (``~/...``), explicit relative (``./...``,
    ``../...``), and bare relative paths (``dir/file.php``).
    """
    if path.startswith(":"):
        return False
    return True


def _copy_to_profile(
    src: str,
    dest: str,
    recursive: bool,
    profile_name: str,
    parents: bool = False,
    timeout: float = 120.0,
) -> int:
    """Copy files to/from a single profile. Returns exit code."""
    config = load_config()
    p = config.get_profile(profile_name)
    runner = PTYRunner()

    src_is_local = _is_local_path(src)

    from ..utils.keychain import prompt_for_credential

    ssh_pw = prompt_for_credential(profile_name, p.user)

    from ..core.pty_engine import _scp_target

    if src_is_local:
        # Local -> remote (dest is on the remote host via profile)
        resolved_src = str(Path(src).expanduser().resolve())
        remote_dest = dest[1:] if dest.startswith(":") else dest  # ":" only marks "remote"
        if parents:
            # Preserve source directory structure: app/Foo/Bar.php -> /remote/app/Foo/Bar.php
            remote_dest = remote_dest.rstrip("/") + "/" + src
            remote_parent = str(Path(remote_dest).parent)
            # Create parent dirs on remote before scp
            mkdir_ssh = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
            mkdir_ssh.append(f"mkdir -p {safe_quote(remote_parent)}")
            if runner.run(mkdir_ssh, password=ssh_pw, timeout=10) != 0:
                console.print(f"[red]  [{profile_name}] Could not create {remote_parent} on the remote[/red]")
                return 1
        scp_args = build_scp_args(
            src=resolved_src,
            dest=_scp_target(remote_dest, p.host, p.user),
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
            recursive=recursive,
        )
    else:
        # Remote -> local (dest is a local path)
        resolved_dest = str(Path(dest).expanduser().resolve())
        scp_args = build_scp_args(
            src=_scp_target(src, p.host, p.user),
            dest=resolved_dest,
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
            recursive=recursive,
        )

    display_dest = dest.rstrip("/") + "/" + src if parents else dest
    console.print(Text(f"[{profile_name}] Copying {src} -> {display_dest}...", style="cyan"))

    exit_code = runner.run(
        scp_args,
        password=ssh_pw,
        on_output=lambda line: _remote(line, f"  [{profile_name}] "),
        timeout=timeout,
    )

    if exit_code == 0:
        console.print(Text(f"  [{profile_name}] Copy complete", style="green"))
    elif exit_code == 124:
        console.print(Text(
            f"  [{profile_name}] Copy timed out after {timeout:g}s and may be incomplete; "
            f"raise the limit with --timeout", style="red"))
    else:
        console.print(Text(f"  [{profile_name}] Copy failed (exit code: {exit_code})", style="red"))

    return exit_code


@click.command()
@click.argument("src")
@click.argument("dest")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories")
@click.option("--parents", is_flag=True, help="Create parent directories on remote")
@click.option("--profile", "-p", multiple=True, default=None, help="Profile name (can be used multiple times)")
@click.option("--all", "all_profiles", is_flag=True, help="Copy to all profiles")
@click.option("--except", "except_profiles", default="", help="Comma-separated profiles to exclude when using --all")
@click.option("--timeout", type=float, default=None, help="Per-profile transfer timeout in seconds (default: 120)")
def cmd_cp(src: str, dest: str, recursive: bool, parents: bool, profile: tuple[str, ...] | None, all_profiles: bool, except_profiles: str, timeout: float | None) -> None:
    """Securely copy files with auto-sudo escalation.

    Supports copying to one or multiple profiles:

    \b
      spyro cp file.txt /remote/ -p staging
      spyro cp file.txt /remote/ --all
      spyro cp file.txt /remote/ --all --except ird-server,production
      spyro cp file.txt /remote/ -p staging -p dev
    """
    config = load_config()

    # Resolve target profiles
    if all_profiles:
        targets = config.profile_names
        if except_profiles:
            exclusions = {n.strip() for n in except_profiles.split(",") if n.strip()}
            targets = [n for n in targets if n not in exclusions]
            if exclusions:
                console.print(f"  Excluding: {', '.join(sorted(exclusions))}")
    elif profile:
        # Split each -p value on commas so "-p staging,dev" works
        targets = []
        for p in profile:
            targets.extend(n.strip() for n in p.split(",") if n.strip())
    else:
        env_profile = os.environ.get("SPYRO_PROFILE", "")
        if env_profile:
            targets = [n.strip() for n in env_profile.split(",") if n.strip()]
        else:
            console.print("[red]Specify at least one --profile/-p, --all, or set SPYRO_PROFILE[/red]")
            return

    if not targets:
        console.print("[red]No profiles matched[/red]")
        return

    console.print(Text(f"Copying to {len(targets)} profile(s): {', '.join(targets)}\n", style="bold cyan"))

    resolved_timeout = _get_timeout(config, timeout, 120.0)
    results: dict[str, int] = {}
    for name in targets:
        ec = _copy_to_profile(src, dest, recursive, name, parents=parents, timeout=resolved_timeout)
        results[name] = ec

    # Summary
    successes = [n for n, ec in results.items() if ec == 0]
    failures = [n for n, ec in results.items() if ec != 0]
    if successes:
        console.print(f"\n[green]✓ Succeeded: {len(successes)} profile(s)[/green]")
    if failures:
        console.print(Text(f"\n✗ Failed: {len(failures)} profile(s) — {', '.join(failures)}", style="red"))
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_tunnel_info(name: str, state: object) -> None:
    """Print tunnel connection info."""
    console.print(f"  [green]✓[/green] {name}: PID {state.pid}")
    for port in state.forwarded_ports:
        console.print(f"    localhost:{port}")


# ---------------------------------------------------------------------------
# spyro auth — Keychain credential management
# ---------------------------------------------------------------------------


@click.group()
def cmd_auth() -> None:
    """Manage stored credentials (macOS Keychain / Linux Secret Service)."""


@cmd_auth.command("set")  # not `def set`: that would shadow the builtin for this whole module
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--password", "-w", default="", help="Password (omit to prompt)")
@click.option("--force", "-f", is_flag=True, help="Overwrite without prompting")
def set_credential(profile: str, password: str, force: bool) -> None:
    """Store a credential in the OS keychain.

    One password per profile — used for both SSH and sudo.
    If --password is omitted, you'll be prompted securely (no echo).
    """
    import getpass
    from ..utils.keychain import store_credential, get_credential

    # Load profile to get username
    config = load_config()
    try:
        p = config.get_profile(profile)
        username = p.user
    except Exception:
        username = profile

    # Check existing
    existing = get_credential(profile, username)
    if existing and not force:
        console.print(f"[yellow]  Credential for {username}@{profile} already exists[/yellow]")
        if not click.confirm(f"  Overwrite?"):
            return

    pw = password or getpass.getpass(f"  password for {username}@{profile}: ")
    if not pw:
        console.print(f"  [red]No password provided, skipping[/red]")
        return

    if store_credential(profile, username, pw):
        console.print(f"[green]  ✓ Credential stored for {username}@{profile}[/green]")
    else:
        console.print(f"[red]  ✗ Failed to store credential[/red]")


@cmd_auth.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def delete(profile: str) -> None:
    """Remove stored credentials from the OS keychain."""
    from ..utils.keychain import delete_credential, get_credential

    config = load_config()
    try:
        p = config.get_profile(profile)
        username = p.user
    except Exception:
        username = profile

    if get_credential(profile, username):
        if delete_credential(profile, username):
            console.print(f"[green]  ✓ Credential deleted for {username}@{profile}[/green]")
        else:
            console.print(f"[red]  ✗ Failed to delete credential[/red]")
    else:
        console.print(f"  [yellow]No credential found for {username}@{profile}[/yellow]")


@cmd_auth.command("list")
def list_credentials() -> None:
    """Show which credentials are stored in the keychain."""
    from ..utils.keychain import SERVICE_NAME
    import keyring

    try:
        # Keyring backends don't support listing passwords directly,
        # so scan known patterns from config if available
        config = load_config()
        profiles = [(name, config.get_profile(name).user) for name in config.profile_names]
    except Exception:
        profiles = []

    found = False

    if profiles:
        from ..utils.keychain import get_credential

        for name, username in profiles:
            pw = get_credential(name, username)
            if pw is not None:
                console.print(Text(f"  ✓ {name}: {username} (password stored, {len(pw)} characters)"))
                found = True

    if not found:
        console.print("[yellow]No credentials stored.[/yellow]")
        if not profiles:
            console.print("[yellow]No spyro.toml found. Run 'spyro auth set -p <profile>' after creating one.[/yellow]")
        else:
            console.print("[yellow]Use: spyro auth set -p <profile>[/yellow]")


# ---------------------------------------------------------------------------
# spyro supervisor — Supervisor process management
# ---------------------------------------------------------------------------



def _run_svc_cmd(
    profile: str,
    cmd: str,
    timeout: float | None = 30.0,
    cli_timeout: float | None = None,
    show_exit_code: bool = True,
    escalate: bool | None = None,
    prefix: str = "  ",
) -> int:
    """Run *cmd* on a profile's server through the PTY engine and print its output.

    *timeout* ``None`` means "until it exits" (``logs -f``). *escalate* says
    whether the command will use sudo (allocates a remote tty so sudo can
    prompt); ``None`` keeps the service-command rule: the profile's ``sudo``.
    """
    config = load_config()
    p = config.get_profile(profile)
    resolved = None if timeout is None else _get_timeout(config, cli_timeout, timeout)

    if escalate is None:
        if not p.sudo and "sudo" in cmd:
            console.print(f"[red]  ✗ User '{p.user}' does not have sudo access on {profile}[/red]")
            console.print("[yellow]  Set sudo = true in your spyro.toml for this profile[/yellow]")
            return 1
        escalate = p.sudo

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    if escalate:
        ssh_args.insert(1, "-t")
    ssh_args.append(cmd)

    from ..utils.keychain import prompt_for_credential

    sudo_pw = prompt_for_credential(profile, p.user) if escalate else ""
    ssh_pw = prompt_for_credential(profile, p.user)

    ec = PTYRunner().run(
        ssh_args, password=ssh_pw, sudo_password=sudo_pw,
        on_output=lambda line: _remote(line, prefix), timeout=resolved,
    )
    if show_exit_code:
        _report_exit(ec, resolved, prefix)
    return ec

@click.group()
def cmd_supervisor() -> None:
    """Manage Supervisor processes on remote server."""


@cmd_supervisor.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Supervisor process status."""
    _run_svc_cmd(profile, "sudo supervisorctl status")


@cmd_supervisor.command()
@click.argument("process", default="all")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--timeout", type=float, default=None, help="Timeout in seconds (default: 60)")
def restart(profile: str, process: str, timeout: float | None) -> None:
    """Restart Supervisor process(es). Default: all."""
    _run_svc_cmd(profile, f"sudo supervisorctl restart {safe_quote(process)}", timeout=60, cli_timeout=timeout)


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
def start(profile: str, process: str) -> None:
    """Start a Supervisor process."""
    _run_svc_cmd(profile, f"sudo supervisorctl start {safe_quote(process)}")


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
def stop(profile: str, process: str) -> None:
    """Stop a Supervisor process."""
    _run_svc_cmd(profile, f"sudo supervisorctl stop {safe_quote(process)}")


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines to tail")
def tail(profile: str, process: str, lines: int) -> None:
    """Tail Supervisor process stderr log."""
    _run_svc_cmd(profile, f"sudo supervisorctl tail -{int(lines)} {safe_quote(process)} 2>/dev/null || sudo supervisorctl tail {safe_quote(process)}")

# ---------------------------------------------------------------------------
# spyro redis — Redis CLI wrapper
# ---------------------------------------------------------------------------


@click.group()
def cmd_redis() -> None:
    """Run Redis commands on remote server."""


@cmd_redis.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def ping(profile: str) -> None:
    """Ping Redis server."""
    _run_svc_cmd(profile, "redis-cli ping", timeout=10)


@cmd_redis.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--section", "-s", default="", help="Info section (server, stats, keyspace, etc.)")
def info(profile: str, section: str) -> None:
    """Show Redis server info."""
    _run_svc_cmd(profile, f"redis-cli info {safe_quote(section)}".strip() if section else "redis-cli info", timeout=10)


@cmd_redis.command()
@click.argument("command", nargs=-1, required=True)
@click.option("--profile", "-p", required=True, help="Profile name")
def cli(profile: str, command: tuple[str, ...]) -> None:
    """Run an arbitrary redis-cli command."""
    _run_svc_cmd(profile, f"redis-cli {' '.join(safe_quote(a) for a in command)}", timeout=10)


@cmd_redis.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def stats(profile: str) -> None:
    """Show Redis key metrics (connections, commands, keyspace)."""
    _run_svc_cmd(profile, 'redis-cli info stats | grep -E "^(total_connections|total_commands|keyspace_|instantaneous)"', timeout=10)

# ---------------------------------------------------------------------------
# spyro php — PHP CLI and FPM management
# ---------------------------------------------------------------------------


@click.group()
def cmd_php() -> None:
    """Manage PHP on remote server."""


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def version(profile: str) -> None:
    """Show PHP version."""
    if _run_svc_cmd(profile, "php -v 2>/dev/null | head -3", timeout=10, show_exit_code=False) != 0:
        _run_svc_cmd(profile, "php --version 2>/dev/null | head -3", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def fpm_status(profile: str) -> None:
    """Show PHP-FPM status (pools, processes)."""
    if _run_svc_cmd(profile, 'php-fpm -tt 2>/dev/null || (echo "PHP-FPM config test:" && pgrep -af "php-fpm" 2>/dev/null || echo "not running")', timeout=10, show_exit_code=False) != 0:
        _run_svc_cmd(profile, "pgrep -af 'php-fpm' 2>/dev/null || echo 'PHP-FPM not running'", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--filter", "-f", default="", help="Filter extensions (grep pattern)")
def extensions(profile: str, filter: str) -> None:
    """List PHP extensions."""
    cmd = "php -m 2>/dev/null | tail -n +2"
    if filter:
        cmd += f" | grep -i {safe_quote(filter)}"
    if _run_svc_cmd(profile, cmd, timeout=10, show_exit_code=False) != 0:
        _run_svc_cmd(profile, "php -m 2>/dev/null || echo 'PHP not available'", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--option", "-o", default="", help="Specific ini option (e.g. memory_limit)")
def info(profile: str, option: str) -> None:
    """Show PHP configuration."""
    cmd = "php -i 2>/dev/null"
    if option:
        cmd += f" | grep -i {safe_quote(option)}"
    _run_svc_cmd(profile, cmd, timeout=15)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--timeout", type=float, default=None, help="Timeout in seconds (default: 30)")
def restart(profile: str, timeout: float | None) -> None:
    """Restart PHP-FPM."""
    _run_svc_cmd(profile, "sudo systemctl restart php*-fpm 2>/dev/null || sudo service php*-fpm restart 2>/dev/null || (echo 'Trying sudo kill -USR2...' && sudo kill -USR2 $(pgrep -f 'php-fpm: master' | head -1) 2>/dev/null || echo 'Could not restart PHP-FPM')", timeout=30, cli_timeout=timeout)

# ---------------------------------------------------------------------------
# spyro apache — Apache web server management
# ---------------------------------------------------------------------------


@click.group()
def cmd_apache() -> None:
    """Manage Apache web server on remote server."""


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def version(profile: str) -> None:
    """Show Apache version."""
    _run_svc_cmd(profile, "apache2 -v 2>/dev/null || httpd -v 2>/dev/null || echo 'Apache not found'", timeout=10)


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def modules(profile: str) -> None:
    """List loaded Apache modules."""
    _run_svc_cmd(profile, "apache2 -M 2>/dev/null || httpd -M 2>/dev/null || echo 'Apache not found'", timeout=10)


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Apache server status."""
    _run_svc_cmd(profile, "apache2ctl status 2>/dev/null || apachectl status 2>/dev/null || (pgrep -x apache2 >/dev/null && echo 'Apache running' || echo 'Apache not running')", timeout=10)


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def sites(profile: str) -> None:
    """List enabled Apache virtual hosts."""
    _run_svc_cmd(profile, "ls -1 /etc/apache2/sites-enabled/ 2>/dev/null || ls -1 /etc/httpd/sites-enabled/ 2>/dev/null || echo 'No sites-enabled found'", timeout=10)


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--timeout", type=float, default=None, help="Timeout in seconds (default: 30)")
def restart(profile: str, timeout: float | None) -> None:
    """Restart Apache."""
    _run_svc_cmd(profile, "sudo systemctl restart apache2 2>/dev/null || sudo systemctl restart httpd 2>/dev/null || sudo service apache2 restart 2>/dev/null || sudo service httpd restart 2>/dev/null || echo 'Could not restart Apache'", timeout=30, cli_timeout=timeout)
# ---------------------------------------------------------------------------
# spyro nginx — Nginx web server management
# ---------------------------------------------------------------------------

@click.group()
def cmd_nginx() -> None:
    """Manage Nginx web server on remote server."""


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def version(profile: str) -> None:
    """Show Nginx version."""
    _run_svc_cmd(profile, "nginx -v 2>&1 || echo 'Nginx not found'", timeout=10)


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Nginx server status."""
    _run_svc_cmd(profile, "nginx -t 2>&1 && (pgrep -x nginx >/dev/null && echo 'Nginx running' || echo 'Nginx not running')", timeout=10)


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def sites(profile: str) -> None:
    """List enabled Nginx site configs."""
    _run_svc_cmd(profile, "ls -1 /etc/nginx/sites-enabled/ 2>/dev/null || ls -1 /etc/nginx/conf.d/ 2>/dev/null || echo 'No site configs found'", timeout=10)


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--timeout", type=float, default=None, help="Timeout in seconds (default: 30)")
def restart(profile: str, timeout: float | None) -> None:
    """Restart Nginx."""
    _run_svc_cmd(profile, "sudo systemctl restart nginx 2>/dev/null || sudo service nginx restart 2>/dev/null || sudo nginx -s reload 2>/dev/null || echo 'Could not restart Nginx'", timeout=30, cli_timeout=timeout)

# ---------------------------------------------------------------------------
# spyro caddy — Caddy web server management
# ---------------------------------------------------------------------------


@click.group()
def cmd_caddy() -> None:
    """Manage Caddy web server on remote server."""


@cmd_caddy.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def version(profile: str) -> None:
    """Show Caddy version."""
    _run_svc_cmd(profile, "caddy version 2>&1 || echo 'Caddy not found'", timeout=10)


@cmd_caddy.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Caddy server status."""
    _run_svc_cmd(profile, "(pgrep -x caddy >/dev/null || pgrep -f 'caddy run' >/dev/null) && (caddy version 2>/dev/null || echo 'running') || echo 'Caddy not running'", timeout=10)


@cmd_caddy.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--timeout", type=float, default=None, help="Timeout in seconds (default: 30)")
def restart(profile: str, timeout: float | None) -> None:
    """Restart Caddy."""
    _run_svc_cmd(profile, "sudo systemctl restart caddy 2>/dev/null || sudo service caddy restart 2>/dev/null || (sudo kill -USR1 $(pgrep -x caddy | head -1) 2>/dev/null && echo 'Sent reload signal') || echo 'Could not restart Caddy'", timeout=30, cli_timeout=timeout)

# ---------------------------------------------------------------------------
# spyro tinker — Laravel Tinker REPL
# ---------------------------------------------------------------------------


def _upload_and_run(
    profile: str,
    local_file: Path,
    build_cmd: "Callable[[str], str]",
    *,
    no_escalate: bool,
    label: str,
    timeout: float = 120.0,
) -> int:
    """scp *local_file* to a private remote /tmp name, run ``build_cmd(remote_path)`` there, then delete it.

    The remote name is unguessable (128 random bits): with sudo the file is
    executed as another user, so a predictable name in /tmp would let anyone
    on a shared server pre-create or swap it.
    """
    import uuid

    from ..core.pty_engine import _scp_target
    from ..utils.keychain import prompt_for_credential

    config = load_config()
    p = config.get_profile(profile)
    runner = PTYRunner()
    ssh_pw = prompt_for_credential(profile, p.user)

    tmp_remote = f"/tmp/spyro-{label}-{uuid.uuid4().hex}.php"
    scp_args = build_scp_args(
        src=str(local_file),
        dest=_scp_target(tmp_remote, p.host, p.user),
        host=p.host, user=p.user, port=p.port, key=p.key,
    )
    console.print(Text(f"Uploading {local_file.name} to {p.host}:{tmp_remote}...", style="cyan"))
    if runner.run(scp_args, password=ssh_pw, timeout=30) != 0:
        console.print(f"[red]Failed to upload {label}[/red]")
        return 1

    try:
        if p.sudo and p.sudo_user and not no_escalate:
            # scp keeps the local mode (eval's temp file is 0600) and sudo_user
            # is a different account than the one that owns the upload.
            runner.run(
                build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
                + [f"chmod 644 {safe_quote(tmp_remote)}"],
                password=ssh_pw, timeout=10,
            )
        command = f"{_capistrano_cd(p.remote_path)} && {_sudo_prefix(p, no_escalate)}{build_cmd(safe_quote(tmp_remote))}"
        return _run_svc_cmd(
            profile, command, timeout=timeout,
            escalate=p.sudo and not no_escalate, prefix="",
        )
    finally:
        clean = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
        clean.append(f"rm -f {safe_quote(tmp_remote)}")
        runner.run(clean, password=ssh_pw, timeout=10)


@click.command()
@click.option("--eval", "-e", default="", help="Evaluate expression and exit")
@click.option("--file", "-f", type=click.Path(exists=True), help="Run PHP file")
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_tinker(eval: str, file: str | None, no_escalate: bool, profile: str) -> None:
    """Run Laravel Tinker interactively or with --eval/--file.

    Examples:
      spyro tinker -p staging                          # Interactive REPL
      spyro tinker -p staging -e "User::count()"       # One-shot eval
      spyro tinker -p staging -f script.php            # Run file
    """
    config = load_config()
    p = config.get_profile(profile)

    if not p.artisan:
        console.print(f"[yellow]Profile '{profile}' is not configured for artisan[/yellow]")
        return

    escalate = p.sudo and not no_escalate

    if file:
        _upload_and_run(
            profile, Path(file).expanduser().resolve(),
            lambda remote: f"php artisan tinker < {remote}",
            no_escalate=no_escalate, label="tinker",
        )
        return

    cd_cmd = _capistrano_cd(p.remote_path)
    if eval:
        tinker_cmd = f"{cd_cmd} && {_sudo_prefix(p, no_escalate)}php artisan tinker --execute={safe_quote(eval)}"
        _run_svc_cmd(profile, tinker_cmd, timeout=120.0, escalate=escalate, prefix="")
        return

    # Interactive REPL: needs a remote tty whether or not sudo is involved
    from ..utils.keychain import prompt_for_credential

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    ssh_args.insert(1, "-t")
    ssh_args.append(f"{cd_cmd} && {_sudo_prefix(p, no_escalate)}php artisan tinker")
    ssh_pw = prompt_for_credential(profile, p.user)
    sudo_pw = ssh_pw if escalate else ""

    console.print(f"[cyan]Starting Tinker on {profile}...[/cyan]")
    console.print("[dim]Exit with Ctrl+D or type 'exit'[/dim]")
    PTYRunner().interactive_run(ssh_args, password=ssh_pw, sudo_password=sudo_pw, timeout=30.0)


# ---------------------------------------------------------------------------
# spyro eval — Evaluate PHP expression on remote Laravel
# ---------------------------------------------------------------------------


def _laravel_alias_php() -> str:
    """Generate PHP code that registers PsySH-style short aliases.

    Reads ``config('app.aliases')`` and calls ``class_alias()`` for each,
    so facades like ``DB``, ``Schema``, ``Cache``, ``Route`` etc. resolve
    without the full namespace. Also scans ``app/Models/`` for Eloquent
    models and aliases them (e.g. ``User`` -> ``App\\Models\\User``).

    Returns an empty string if the Laravel app doesn't have aliases
    configured (safe to concatenate unconditionally).
    """
    p = '\\Illuminate\\Support\\Facades\\App'
    return (
        "// Register PsySH-style short aliases from config(app.aliases)\n"
        f"$aliases = {p}::make('config')->get('app.aliases', []);\n"
        "if (is_array($aliases)) {\n"
        "    foreach ($aliases as $alias => $fqcn) {\n"
        "        if (is_string($alias) && is_string($fqcn)\n"
        "            && !class_exists($alias, false)) {\n"
        "            class_alias($fqcn, $alias);\n"
        "        }\n"
        "    }\n"
        "}\n"
        "\n"
        "// Auto-import models from app/Models/\n"
        f"$modelsDir = {p}::make('path') . '/Models';\n"
        "if (is_dir($modelsDir)) {\n"
        "    foreach (glob($modelsDir . '/*.php') as $modelFile) {\n"
        "        $className = basename($modelFile, '.php');\n"
        "        $fqcn = 'App\\\\Models\\\\' . $className;\n"
        "        if (!class_exists($className, false)) {\n"
        "            class_alias($fqcn, $className);\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


def build_eval_php(expression: str, json_output: bool = False, no_aliases: bool = False) -> str:
    """Build a PHP script that boots Laravel and evaluates *expression*.

    The script boots Laravel via bootstrap/app.php, evaluates the expression
    inside a closure, and echoes the result via ``print_r`` or ``json_encode``.

    Args:
        expression: PHP expression to evaluate (e.g. ``User::count()``).
        json_output: If True, wrap result in ``json_encode()`` instead of
            ``print_r``.
        no_aliases: If True, skip registering PsySH-style short aliases
            (``DB``, ``Schema``, model classes, etc.).

    Returns:
        Complete PHP source code as a string.
    """
    if json_output:
        output_expr = (
            f"json_encode((function() {{ return {expression}; }})(),"
            " JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)"
        )
    else:
        output_expr = f"print_r((function() {{ return {expression}; }})(), true)"

    alias_code = "" if no_aliases else _laravel_alias_php()

    return (
        "<?php\n"
        "\n"
        "$base = getcwd();\n"
        "require $base . '/vendor/autoload.php';\n"
        "$app = require_once $base . '/bootstrap/app.php';\n"
        "$kernel = $app->make(Illuminate\\Contracts\\Console\\Kernel::class);\n"
        "$kernel->bootstrap();\n"
        "\n"
        + alias_code + "\n"
        "echo " + output_expr + ";\n"
        "echo \"\\n\";\n"
    )


@click.command()
@click.argument("expression")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-aliases", is_flag=True, help="Skip PsySH-style short aliases (require full namespaces)")
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_eval(expression: str, json_output: bool, no_aliases: bool, no_escalate: bool, profile: str) -> None:
    """Evaluate a PHP expression on the remote Laravel server.

    Boots Laravel via bootstrap/app.php and evaluates the expression directly
    with php CLI (no PsySH). By default, registers PsySH-style short aliases
    (``DB``, ``Schema``, ``Cache``, model classes, etc.) so you can use short
    class names. Use ``--no-aliases`` to require full namespaces.

    Outputs the result reliably. Avoids the quoting and output-capture
    issues of `spyro tinker -e`.

    Examples:

      spyro eval 'User::count()' -p staging

      spyro eval 'DB::table("users")->count()' -p staging --json

      spyro eval 'BillExchange::where("enabled", true)->count()' -p staging
    """
    config = load_config()
    p = config.get_profile(profile)

    if not p.artisan:
        console.print(f"[yellow]Profile '{profile}' is not configured for artisan[/yellow]")
        return

    if not p.remote_path:
        console.print(f"[red]Profile '{profile}' has no remote_path configured[/red]")
        return

    import tempfile

    # mkstemp creates the file 0600: the expression may contain sensitive data
    fd, tmp_name = tempfile.mkstemp(prefix="spyro-eval-", suffix=".php")
    tmp_local = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(build_eval_php(expression, json_output, no_aliases))
        _upload_and_run(
            profile, tmp_local, lambda remote: f"php {remote}",
            no_escalate=no_escalate, label="eval",
        )
    finally:
        tmp_local.unlink(missing_ok=True)


@click.command()
@click.argument("file", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
def cmd_script(file: str, profile: str, no_escalate: bool) -> None:
    """Upload a PHP file and execute it in the remote Laravel context.

    Reads a local .php file, uploads it to the remote server, executes it
    with ``php`` from the configured ``remote_path``, prints the output,
    and cleans up the remote temp file. The local file is untouched.

    Examples:

      spyro script fix_stuck_users.php -p staging

      spyro script app/scripts/deploy_hook.php -p production
    """
    config = load_config()
    p = config.get_profile(profile)

    if not p.remote_path:
        console.print(f"[red]Profile '{profile}' has no remote_path configured[/red]")
        return

    file_path = Path(file).expanduser().resolve()
    if not file_path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        return

    _upload_and_run(
        profile, file_path, lambda remote: f"php {remote}",
        no_escalate=no_escalate, label="script",
    )


# ---------------------------------------------------------------------------
# spyro db — Database commands (MySQL/MariaDB/PostgreSQL)
# ---------------------------------------------------------------------------


def _detect_db_client(profile: str) -> str:
    """Detect which database client is available locally (mysql, mariadb, psql)."""
    for client in ["mariadb", "mysql", "psql"]:
        if shutil.which(client):
            return client
    return "mysql"


def _run_db_query(profile: str, query: str, tunnel_port: int | None = None) -> tuple[int, str]:
    """Run a SQL query through the tunnel and return (exit_code, output)."""
    _, db, local_port = _db_target(profile, port=tunnel_port)
    client = _detect_db_client(profile)
    try:
        result = subprocess.run(
            client_argv(client, db, local_port, query),
            capture_output=True, text=True, timeout=30, env=client_env(db, local_port),
        )
    except FileNotFoundError:
        return 1, "Client not found. Install mysql-client, mariadb-client, or postgresql-client."
    except subprocess.TimeoutExpired:
        return 1, "Query timed out"
    if result.returncode == 0:
        return 0, result.stdout
    return result.returncode, (result.stderr or result.stdout).strip()


@click.group(invoke_without_command=True)
@click.pass_context
def cmd_db(ctx: click.Context) -> None:
    """Database commands (MySQL/MariaDB/PostgreSQL)."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def tunnel(port: int | None, profile: str) -> None:
    """Start tunnel and print connection URL."""
    p, db, local_port = _db_target(profile, port=port)
    console.print("\n[bold green]Database tunnel active[/bold green]")
    console.print(f"  Profile:   {profile}")
    console.print(f"  Local:     127.0.0.1:{local_port}")
    console.print(f"  Remote:    {p.host}:{db.port}")
    console.print(Text(f"  URL:       {generate_connection_url(db, port_override=local_port)}"))


@cmd_db.command()
@click.option("--no-tunnel", is_flag=True, help="Skip tunnel management")
@click.option("--profile", "-p", required=True, help="Profile name")
def shell(no_tunnel: bool, profile: str) -> None:
    """Launch pre-authenticated database CLI (mysql/mariadb/psql)."""
    _, db, local_port = _db_target(profile, no_tunnel=no_tunnel)
    client = _detect_db_client(profile)
    if not shutil.which(client):
        raise click.ClickException("No database client found. Install mysql, mariadb, or psql.")
    console.print(f"[cyan]Connecting to {db.name} via {client}...[/cyan]")
    os.execvpe(client, client_argv(client, db, local_port), client_env(db, local_port))


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def ping(port: int | None, profile: str) -> None:
    """Test database connectivity through the tunnel."""
    p, db, local_port = _db_target(profile, port=port)
    client = _detect_db_client(profile)
    console.print(f"[cyan]Pinging {db.driver}@{p.host}:{db.port} via 127.0.0.1:{local_port}...[/cyan]")
    env = client_env(db, local_port)
    if client in ("mysql", "mariadb"):
        cmd = ["mysqladmin", "-h127.0.0.1", f"-P{local_port}", f"-u{db.user}", "--skip-ssl", "ping", "--silent"]
        ok_text, missing = "✓ mysqld is alive", "mysqladmin not found locally"
    else:
        cmd = ["psql", "-c", "SELECT 1"]
        ok_text, missing = "✓ PONG", "psql not found locally"
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, env=env)
    except FileNotFoundError:
        raise click.ClickException(missing) from None
    except subprocess.TimeoutExpired:
        raise click.ClickException("Ping timed out") from None
    stdout, stderr = result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")
    if result.returncode == 0 or "mysqld is alive" in stdout:
        console.print(f"[green]{ok_text}[/green]")
    elif "Access denied" in stderr:
        raise click.ClickException("Access denied")
    else:
        raise click.ClickException(f"Ping failed: {stderr.strip()[:200]}")


@cmd_db.command()
@click.argument("query")
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def query(profile: str, query: str, port: int | None) -> None:
    """Run a SQL query through the tunnel."""
    ec, output = _run_db_query(profile, query, tunnel_port=port)
    if ec != 0:
        raise click.ClickException(output)
    for line in output.splitlines():
        _remote(line)


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def list_databases(port: int | None, profile: str) -> None:
    """List databases on the remote server."""
    sql = "\\l" if _detect_db_client(profile) == "psql" else "SHOW DATABASES"
    ec, output = _run_db_query(profile, sql, tunnel_port=port)
    if ec != 0:
        raise click.ClickException(output)
    for line in output.splitlines():
        _remote(line)


# ---------------------------------------------------------------------------
# spyro logs — Remote log viewing (Laravel, Nginx, Apache, PHP)
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.pass_context
def cmd_logs(ctx: click.Context) -> None:
    """View remote logs (Laravel, Nginx, Apache, PHP-FPM).

    Subcommands:
      laravel      Tail the Laravel log file
      nginx        Tail Nginx access log
      nginx-error  Tail Nginx error log
      apache       Tail Apache access log
      php          Tail PHP-FPM log
      supervisor   Tail Spyro supervisor tunnel log (default)
    """
    if ctx.invoked_subcommand is None:
        from ..utils.paths import spyro_home
        log_dir = spyro_home() / "logs"
        if not log_dir.exists() or not any(log_dir.iterdir()):
            console.print("[yellow]No supervisor log files found[/yellow]")
            console.print("Try: spyro logs laravel -p staging  or  spyro logs supervisor staging")
            return
        console.print("[cyan]Available supervisor logs:[/cyan]")
        for log_file in sorted(log_dir.glob("*.log")):
            console.print(f"  {log_file.stem}.log")
        console.print("\n[yellow]Use: spyro logs supervisor <profile> [-f][/yellow]")


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def laravel(profile: str, lines: int, follow: bool) -> None:
    """Tail the Laravel log file."""
    config = load_config()
    p = config.get_profile(profile)
    log_path = safe_quote(f"{p.remote_path}/storage/logs/laravel.log")
    tail_flag = " -f" if follow else ""
    _run_svc_cmd(profile, f"tail -n {int(lines)}{tail_flag} {log_path} 2>/dev/null || echo 'Log not found'", timeout=None if follow else 15)


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def nginx(profile: str, lines: int, follow: bool) -> None:
    """Tail Nginx access log."""
    tail_flag = " -f" if follow else ""
    _run_svc_cmd(profile, f"tail -n {int(lines)}{tail_flag} /var/log/nginx/access.log 2>/dev/null || tail -n {int(lines)}{tail_flag} /var/log/nginx/*access* 2>/dev/null || echo 'Nginx access log not found'", timeout=None if follow else 15)


@cmd_logs.command(name="nginx-error")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def nginx_error(profile: str, lines: int, follow: bool) -> None:
    """Tail Nginx error log."""
    tail_flag = " -f" if follow else ""
    _run_svc_cmd(profile, f"tail -n {int(lines)}{tail_flag} /var/log/nginx/error.log 2>/dev/null || echo 'Nginx error log not found'", timeout=None if follow else 15)


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def apache(profile: str, lines: int, follow: bool) -> None:
    """Tail Apache access log."""
    tail_flag = " -f" if follow else ""
    _run_svc_cmd(profile, f"tail -n {int(lines)}{tail_flag} /var/log/apache2/access.log 2>/dev/null || tail -n {int(lines)}{tail_flag} /var/log/httpd/access_log 2>/dev/null || echo 'Apache access log not found'", timeout=None if follow else 15)


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def php(profile: str, lines: int, follow: bool) -> None:
    """Tail PHP-FPM error log."""
    tail_flag = " -f" if follow else ""
    _run_svc_cmd(profile, f"tail -n {int(lines)}{tail_flag} /var/log/php*-fpm.log 2>/dev/null || tail -n {int(lines)}{tail_flag} /var/log/php*.log 2>/dev/null || echo 'PHP-FPM log not found'", timeout=None if follow else 15)


@cmd_logs.command()
@click.argument("profile_name", required=True)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def supervisor(profile_name: str, follow: bool) -> None:
    """Tail Spyro supervisor tunnel log."""
    from ..utils.paths import spyro_home
    log_file = spyro_home() / "logs" / f"{profile_name}.log"
    if not log_file.exists():
        console.print(f"[red]No supervisor log for '{profile_name}'[/red]")
        return
    _show_log(log_file, follow)


@cmd_db.command()
@click.option("--tables", "-t", default="", help="Comma-separated tables (e.g. users,posts)")
@click.option("--output", "-o", default="", help="Output path (default: ./<profile>-<db>-<timestamp>.sql)")
@click.option("--gzip", "-z", is_flag=True, help="Compress with gzip")
@click.option("--no-data", "-d", is_flag=True, help="Schema only, no data")
@click.option("--where", "-w", default="", help="WHERE clause for row filter")
@click.option("--profile", "-p", required=True, help="Profile name")
def dump(profile: str, tables: str, output: str, gzip: bool, no_data: bool, where: str) -> None:
    """Dump remote database to local file.

    \b
    Examples:
      spyro db dump -p staging                          # Full dump
      spyro db dump -p staging -t users,posts           # Specific tables
      spyro db dump -p staging -t users -w "id > 100"   # Filtered rows
      spyro db dump -p staging -z                       # Gzipped
      spyro db dump -p staging -d                       # Schema only
      spyro db dump -p staging -o ./backups/latest.sql  # Custom path
    """
    import gzip as gz
    from datetime import datetime

    config = load_config()
    p = config.get_profile(profile)
    db = _resolve_db(p, profile)
    table_list = [t.strip() for t in tables.split(",") if t.strip()]

    if db.driver not in ("mysql", "mariadb"):
        raise click.ClickException(f"Dump not yet supported for driver: {db.driver}")

    # mysqldump runs on the remote server. Every argument is shell-quoted, and
    # the password is fed through stdin into MYSQL_PWD rather than put on a
    # command line that `ps` (on either machine) can read.
    parts = [
        "mysqldump", f"-h{db.host}", f"-P{db.port}", f"-u{db.user}",
        "--single-transaction", "--quick", "--no-tablespaces", "--routines", "--triggers",
    ]
    if no_data:
        parts.append("--no-data")
    if where:
        parts += ["--where", where]
    parts.append(db.name)
    parts += table_list
    remote_cmd = " ".join(safe_quote(a) for a in parts)
    if db.password:
        remote_cmd = "IFS= read -r MYSQL_PWD && export MYSQL_PWD && " + remote_cmd
    # Whatever the account's login shell is (csh and fish reject `IFS=` and
    # `&&` chains), run it under POSIX sh.
    remote_cmd = f"sh -c {safe_quote(remote_cmd)}"

    if output:
        output_path = Path(output).expanduser().resolve()
        if gzip and not output_path.name.endswith(".gz"):
            output_path = output_path.with_name(output_path.name + ".gz")
    else:
        tbl_suffix = f"-{'-'.join(table_list)}" if table_list else ""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.cwd() / f"{profile}-{db.name}{tbl_suffix}-{ts}{'.sql.gz' if gzip else '.sql'}"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(Text(f"Dumping '{db.name}' from {p.host}...", style="cyan"))
    if table_list:
        console.print(Text(f"  Tables: {', '.join(table_list)}"))
    if where:
        console.print(Text(f"  WHERE: {where}"))
    console.print(Text(f"  Output: {output_path}"))

    # 1. Authenticate through the PTY engine (keychain password). That opens an
    #    ssh ControlMaster, which the next step reuses without any password.
    from ..utils.keychain import prompt_for_credential

    base = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    ec = PTYRunner().run(
        base + ["true"], password=prompt_for_credential(profile, p.user),
        on_output=lambda line: _remote(line, "  "), timeout=30.0,
    )
    if ec != 0:
        raise click.ClickException(f"Could not connect to {p.host} (exit code: {ec})")

    # 2. Stream the dump as raw bytes. A PTY would rewrite newlines, strip
    #    control bytes, merge stderr into the data and buffer it all in memory.
    #    BatchMode is listed first because ssh keeps the first value it sees.
    import tempfile

    ssh_args = list(base)
    ssh_args[1:1] = ["-o", "BatchMode=yes", "-o", "Compression=yes"]
    ssh_args.append(remote_cmd)

    part = output_path.with_name(output_path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # dumps hold real data
    raw = os.fdopen(fd, "wb")
    sink = gz.GzipFile(fileobj=raw, mode="wb") if gzip else raw
    proc = None
    try:
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(ssh_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
            if db.password:
                proc.stdin.write(db.password.encode() + b"\n")
            proc.stdin.close()
            shutil.copyfileobj(proc.stdout, sink, 1 << 20)
            rc = proc.wait()
            err.seek(0)
            err_text = err.read().decode(errors="replace").strip()
        sink.close()
        raw.close()
        if rc != 0 or part.stat().st_size == 0:
            raise click.ClickException(
                f"Dump failed (exit code: {rc})" + (f": {err_text.splitlines()[-1]}" if err_text else "")
            )
        os.replace(part, output_path)
    except BaseException:
        if proc and proc.poll() is None:
            proc.kill()
        for f in (sink, raw):
            try:
                f.close()
            except Exception:
                pass
        part.unlink(missing_ok=True)
        raise

    size = output_path.stat().st_size
    size_str = f"{size/1024:.1f} KB" if size < 1024**2 else f"{size/1024**2:.1f} MB"
    console.print("[green]✓ Dump complete[/green]")
    console.print(Text(f"  File:  {output_path}"))
    console.print(f"  Size:  {size_str}")


# ---------------------------------------------------------------------------
# Version check (shared between notify and update)
# ---------------------------------------------------------------------------


_CHECK_CACHE = None  # (needs_update, latest_tag) cached per process


def _fetch_latest_version(timeout: float = 15) -> str | None:
    """Fetch the latest semver tag from GitHub.

    Returns the version string (e.g. "0.8.7") or None on failure.
    """
    import json
    import urllib.request
    import urllib.error

    GITHUB_API = "https://api.github.com/repos/peterson-umoke/spyro-cli"

    try:
        req = urllib.request.Request(
            f"{GITHUB_API}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "spyro-cli"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            release = json.loads(resp.read().decode())
        latest_tag = release.get("tag_name", "").lstrip("v")
        if latest_tag:
            return latest_tag
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No releases yet — fall back to listing tags
            try:
                tag_req = urllib.request.Request(
                    f"{GITHUB_API}/tags?per_page=10",
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "spyro-cli"},
                )
                with urllib.request.urlopen(tag_req, timeout=timeout) as resp:
                    tags = json.loads(resp.read().decode())
                if not tags:
                    return None
                versions = []
                for t in tags:
                    raw = t.get("name", "").lstrip("v")
                    parts = raw.split(".")
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        versions.append(raw)
                if versions:
                    versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
                    return versions[-1]
            except Exception:
                return None
        return None
    except Exception:
        return None
    return None


def _compare_versions(current: str, latest: str) -> bool:
    """Return True if *latest* > *current* (strict semver comparison)."""
    try:
        current_parts = tuple(int(x) for x in current.split("."))
        latest_parts = tuple(int(x) for x in latest.split("."))
        return current_parts < latest_parts
    except (ValueError, AttributeError):
        return False


def notify_update() -> None:
    """Print a one-line notice on *stderr* if a newer release exists.

    Runs after the command, only when stderr is a terminal (so scripts and
    ``--json`` pipelines never see it or wait for it). The answer is cached for
    24h; a failed check is cached for 1h so being offline costs one short
    timeout, not one per command.
    """
    import json
    import time

    from .. import __version__ as current_version
    global _CHECK_CACHE

    if not sys.stderr.isatty():
        return

    def show(needs_update: bool, latest: str) -> None:
        if needs_update:
            err_console.print(f"[yellow]Update available: v{current_version} → v{latest}[/yellow]")
            err_console.print("  Run [bold]spyro update[/bold] to upgrade.")

    # Per-process cache
    if _CHECK_CACHE is not None:
        show(*_CHECK_CACHE)
        return

    # Disk cache
    from ..utils.paths import spyro_home
    cache_path = spyro_home() / "version_check"
    now = time.time()

    try:
        if cache_path.exists():
            data = json.loads(cache_path.read_text())
            if now - data.get("checked_at", 0) < data.get("ttl", 86400):
                _CHECK_CACHE = (data.get("needs_update", False), data.get("latest_version", ""))
                show(*_CHECK_CACHE)
                return
    except Exception:
        pass

    latest_tag = _fetch_latest_version(timeout=3)
    needs_update = latest_tag is not None and _compare_versions(current_version, latest_tag)
    _CHECK_CACHE = (needs_update, latest_tag or "")

    try:
        cache_path.write_text(json.dumps({
            "checked_at": now,
            "ttl": 86400 if latest_tag else 3600,
            "needs_update": needs_update,
            "latest_version": latest_tag or "",
        }))
    except Exception:
        pass

    show(*_CHECK_CACHE)


# ---------------------------------------------------------------------------
# spyro update
# ---------------------------------------------------------------------------


@click.command()
@click.option("--force", "-f", is_flag=True, help="Force reinstall even if already up to date")
@click.option("--check", is_flag=True, help="Only check for updates, don't install")
def cmd_update(force: bool, check: bool) -> None:
    """Self-update spyro to the latest version from GitHub.

    Checks the repository for the latest tag via the GitHub API, compares it
    against the currently installed version, and reinstalls via ``uv tool
    install --reinstall`` from the canonical git URL.

    Works whether spyro was installed with ``uv tool install`` or via pip.
    The release lookup uses the GitHub REST API, but installing clones the
    repository, so ``git`` must be on PATH.
    """
    from .. import __version__ as current_version

    console.print("[bold cyan]Spyro Update[/bold cyan]\n")
    console.print(f"  Installed: v{current_version}\n")

    GIT_BASE = "git+https://github.com/peterson-umoke/spyro-cli"

    # ── Fetch latest tag from GitHub API ──────────────────────────────────
    console.print("[cyan]Checking for updates...[/cyan]")
    latest_tag = _fetch_latest_version()
    if latest_tag is None:
        console.print("[red]Could not determine latest version from GitHub[/red]")
        return

    console.print(f"  Remote latest: v{latest_tag}")

    # ── Compare versions ──────────────────────────────────────────────────
    needs_update = _compare_versions(current_version, latest_tag)

    if not needs_update:
        console.print(f"\n[green]✓ spyro is already up to date (v{current_version})[/green]")
        if not force:
            return
        console.print("[yellow]   --force: reinstalling anyway...[/yellow]\n")

    if check:
        if needs_update:
            console.print(f"\n[yellow]Update available: v{current_version} → v{latest_tag}[/yellow]")
            console.print("Run [bold]spyro update[/bold] to upgrade.")
        return

    # ── Reinstall via uv tool ─────────────────────────────────────────────
    console.print("\n[cyan]Installing latest version...[/cyan]")

    install_url = f"{GIT_BASE}@v{latest_tag}"

    uv = shutil.which("uv")
    if uv:
        pip_or_uv = [uv, "tool", "install", "--reinstall", install_url]
        label = "uv"
    else:
        pip = shutil.which("pip") or shutil.which("pip3") or "pip3"
        pip_or_uv = [pip, "install", "--upgrade", install_url]
        label = "pip"

    try:
        install = subprocess.run(
            pip_or_uv,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if install.returncode != 0:
            console.print(f"[red]Installation failed:[/red]")
            console.print(f"  {install.stderr.strip()}")
            return
        # Invalidate cache after successful update
        from ..utils.paths import spyro_home
        cache_path = spyro_home() / "version_check"
        try:
            cache_path.unlink()
        except Exception:
            pass
        global _CHECK_CACHE
        _CHECK_CACHE = None
        console.print(f"[green]✓ spyro updated to v{latest_tag}[/green]")
        console.print(f"  (via {label} — you may need to restart your shell)")
    except subprocess.TimeoutExpired:
        console.print("[red]Installation timed out[/red]")
        return


# ---------------------------------------------------------------------------
# spyro ssh / spyro shell
# ---------------------------------------------------------------------------


def _interactive_ssh(profile: str) -> None:
    """Open an interactive SSH session for the given profile."""
    config = load_config()
    p = config.get_profile(profile)

    runner = PTYRunner()

    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )
    # Force PTY allocation for interactive session
    ssh_args.insert(1, "-t")
    # Don't append a command — SSH opens an interactive shell

    from ..utils.keychain import prompt_for_credential

    sudo_pw = prompt_for_credential(profile, p.user) if p.sudo else ""
    ssh_pw = prompt_for_credential(profile, p.user)

    console.print(f"[cyan]Connecting to {p.host} ({profile})...[/cyan]")

    exit_code = runner.interactive_run(
        ssh_args,
        password=ssh_pw,
        sudo_password=sudo_pw,
        timeout=30.0,
    )

    if exit_code == 124:
        console.print("\n[red]Timed out while logging in (30s)[/red]")
    elif exit_code != 0:
        console.print(f"\n[red]Session exited with code: {exit_code}[/red]")


@click.command()
@click.option("--profile", "-p", default=None, help="Profile name (auto-detects if only one exists)")
def cmd_ssh(profile: str | None) -> None:
    """Open an interactive SSH session for a profile.\n
    Uses keychain-stored credentials and handles auth automatically.
    """
    profile = resolve_profile(profile)
    _interactive_ssh(profile)


@click.command()
@click.option("--profile", "-p", default=None, help="Profile name (auto-detects if only one exists)")
def cmd_shell(profile: str | None) -> None:
    """Alias for spyro ssh — open an interactive remote shell."""
    profile = resolve_profile(profile)
    _interactive_ssh(profile)


# ---------------------------------------------------------------------------
# spyro config — Configuration management
# ---------------------------------------------------------------------------


@click.group()
def cmd_config() -> None:
    """Manage spyro configuration."""
@cmd_config.command(name="validate")
@click.option("--resolve", is_flag=True, help="DNS resolve each host")
def config_validate(resolve: bool) -> None:
    """Validate spyro.toml schema for correctness."""
    from ..utils.config import parse_config, parse_ssh_config

    issues: list[str] = []
    warnings: list[str] = []

    try:
        config = load_config()
    except SystemExit as e:
        console.print(f"[red]✗ Could not load config: {escape(str(e))}[/red]")
        return

    console.print("[bold cyan]Spyro Config Validate[/bold cyan]\n")

    if not config.profiles:
        console.print("[red]No profiles defined in spyro.toml[/red]")
        return

    for name, profile in config.profiles.items():
        console.print(f"[bold]Checking profile:[/bold] {name}")

        # Required fields
        if not profile.host:
            issues.append(f"[{name}] host is required")

        if not profile.user:
            issues.append(f"[{name}] user is required")

        # Port range
        if not (1 <= profile.port <= 65535):
            issues.append(f"[{name}] port {profile.port} out of range (1-65535)")

        # DNS resolution (only when --resolve is passed)
        if resolve and profile.host:
            try:
                socket.getaddrinfo(profile.host, profile.port, type=socket.SOCK_STREAM)
                console.print(f"  [green]  ✓[/green] {profile.host}:{profile.port} resolves")
            except socket.gaierror as e:
                issues.append(f"[{name}] {profile.host}:{profile.port} — {e}")
                console.print(f"  [red]  ✗[/red] {profile.host}:{profile.port} — {escape(str(e))}")

        # SSH key existence
        if profile.key:
            key_path = Path(profile.key).expanduser()
            if not key_path.exists():
                warnings.append(f"[{name}] SSH key not found: {profile.key}")

        # Remote path
        if not profile.remote_path:
            warnings.append(f"[{name}] remote_path is empty")

        # Forwarded ports
        for fp in profile.forwarded_ports:
            if not (1 <= fp <= 65535):
                issues.append(f"[{name}] forwarded_port {fp} out of range (1-65535)")

        # DB config
        if profile.db.name and not profile.db.host:
            warnings.append(f"[{name}] db.host is empty")

    # Duplicate forwarded ports across profiles (the second tunnel gets the next free local port)
    all_ports: dict[int, str] = {}
    for n, prof in config.profiles.items():
        for fp in prof.forwarded_ports:
            if fp in all_ports and all_ports[fp] != n:
                warnings.append(
                    f"Port {fp} forwarded in both '{all_ports[fp]}' and '{n}' "
                    f"(the second tunnel will use another local port)"
                )
            all_ports[fp] = n

    # SSH config integration check
    ssh_config = parse_ssh_config()
    if ssh_config:
        console.print(f"\n  [dim]~/.ssh/config: {len(ssh_config)} Host block(s) parsed[/dim]")
        for name in config.profile_names:
            if name in ssh_config:
                console.print(f"  [green]  ✓[/green] Profile '{name}' matches SSH Host block")
    else:
        console.print("\n  [dim]~/.ssh/config: not found or empty[/dim]")

    # Summary
    console.print(f"\n[bold]Issues:[/bold] {len(issues)}")
    for issue in issues:
        console.print(f"  [red]✗ {issue}[/red]")

    console.print(f"[bold]Warnings:[/bold] {len(warnings)}")
    for warning in warnings:
        console.print(f"  [yellow]⚠ {warning}[/yellow]")

    if not issues and not warnings:
        console.print("\n[green]✓ Configuration looks good[/green]")
    elif not issues:
        console.print("\n[yellow]Configuration valid with warnings[/yellow]")
    else:
        console.print("\n[red]Configuration has errors that must be fixed[/red]")


# ---------------------------------------------------------------------------
# spyro ps — Remote process listing
# ---------------------------------------------------------------------------


@click.command()
@click.option("--profile", "-p", default=None, help="Profile name (auto-detects if only one exists)")
@click.option("--grep", "-g", default="", help="Filter processes (grep pattern)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def cmd_ps(profile: str | None, grep: str, json_output: bool) -> None:
    """List processes on remote server."""
    profile = resolve_profile(profile)
    config = load_config()
    p = config.get_profile(profile)

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    # `ps aux` for people; a fixed, header-less column set (args last) for JSON.
    ps_cmd = (
        "ps -eo user=,pid=,pcpu=,pmem=,vsz=,rss=,tty=,stat=,args=" if json_output else "ps aux"
    )
    if grep:
        ps_cmd += f" | grep -i -- {safe_quote(grep)}"
    ssh_args.append(ps_cmd)

    try:
        result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        console.print("[red]Process list timed out[/red]")
        return
    except FileNotFoundError:
        console.print("[red]ssh not found[/red]")
        return

    output = result.stdout.strip()
    # grep exits 1 when nothing matched; that is not an ssh/ps failure
    if result.returncode != 0 and not (grep and result.returncode == 1 and not result.stderr.strip()):
        stderr = result.stderr.strip()
        if stderr:
            console.print(Text(stderr, style="red"))
        return

    if not output:
        console.print("[yellow]No matching processes[/yellow]")
        return

    if json_output:
        keys = ("user", "pid", "cpu", "mem", "vsz", "rss", "tty", "stat", "command")
        entries = [
            dict(zip(keys, line.split(None, 8)))
            for line in output.splitlines()
            if len(line.split(None, 8)) == 9
        ]
        _emit_json(entries)
    else:
        for line in output.splitlines():
            _remote(line)


# ---------------------------------------------------------------------------
# spyro env — Remote environment management
# ---------------------------------------------------------------------------


@click.group()
def cmd_env() -> None:
    """Manage remote environment files.

    Subcommands:
      pull    Download .env from remote
      diff    Compare local and remote .env files
      push    Upload local .env to remote
    """


@cmd_env.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def diff(profile: str) -> None:
    """Compare local .env with remote .env."""
    import difflib

    config = load_config()
    p = config.get_profile(profile)

    # Pull remote .env
    console.print(f"[cyan]Fetching remote .env from {p.host}...[/cyan]")
    from ..utils.keychain import prompt_for_credential

    remote_text = fetch_remote_file(
        p, f"{p.remote_path}/.env", password=prompt_for_credential(profile, p.user)
    )
    if remote_text is None:
        raise click.ClickException("Failed to pull remote .env")

    # Read local .env
    local_path = Path.cwd() / ".env"
    if not local_path.exists():
        console.print("[yellow]No local .env found — showing remote .env only:[/yellow]")
        for line in remote_text.splitlines():
            _remote(line)
        return

    local_text = local_path.read_text(encoding="utf-8")

    # Diff
    diff = list(difflib.unified_diff(
        local_text.splitlines(keepends=True), remote_text.splitlines(keepends=True),
        fromfile=f"{local_path.name} (local)",
        tofile=f".env ({p.host} remote)",
        lineterm="",
    ))

    if not diff:
        console.print("[green]✓ Local and remote .env are identical[/green]")
        return

    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("+") and not line.startswith("+++"):
            style = "green"
        elif line.startswith("-") and not line.startswith("---"):
            style = "red"
        elif line.startswith("@@"):
            style = "cyan"
        else:
            style = ""
        console.print(Text(line, style=style), soft_wrap=True)


@cmd_env.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.argument("source", default=".env", required=False)
def push(profile: str, source: str) -> None:
    """Push local .env file to remote server."""
    from ..core.pty_engine import _scp_target

    src = Path(source).expanduser().resolve()
    if not src.exists():
        console.print(f"[red]Local file not found: {source}[/red]")
        return

    config = load_config()
    p = config.get_profile(profile)

    remote_dest = f"{p.remote_path}/.env"
    remote_scp = _scp_target(remote_dest, p.host, p.user)

    scp_args = build_scp_args(
        src=str(src),
        dest=remote_scp,
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
        recursive=False,
    )

    from ..utils.keychain import prompt_for_credential
    ssh_pw = prompt_for_credential(profile, p.user)

    runner = PTYRunner()
    console.print(f"[cyan]Uploading {source} to {p.host}:{remote_dest}...[/cyan]")
    ec = runner.run(scp_args, password=ssh_pw, timeout=30.0)

    if ec == 0:
        console.print(f"[green]✓ .env pushed to {profile}[/green]")
    else:
        raise click.ClickException(f"Push failed (exit code: {ec})")

