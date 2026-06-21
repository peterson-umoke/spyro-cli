"""CLI command implementations for spyro."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from ..utils.config import (
    DatabaseConfig,
    SpyroConfig,
    generate_config,
    load_config,
    resolve_profile,
)
from ..core.db import generate_connection_url, resolve_db_credentials
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
)
from ..supervisor.tunnel import TunnelManager, TunnelSupervisor
from ..utils.paths import safe_quote

console = Console()
log = logging.getLogger("spyro")


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
@click.option("--no-daemon", is_flag=True, help="Run in foreground")
def cmd_up(profile: str | None, no_daemon: bool) -> None:
    """Start tunnels for a profile (or all if omitted)."""
    config = load_config()
    manager = TunnelManager(config)

    profiles = [profile] if profile else config.profile_names

    for name in profiles:
        try:
            console.print(f"[cyan]Starting tunnel: {name}[/cyan]")
            state = manager.start(name, foreground=no_daemon)
            _print_tunnel_info(name, state)
        except Exception as e:
            console.print(f"[red]Failed to start '{name}': {e}[/red]")


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
    import json as json_mod

    config = load_config()
    manager = TunnelManager(config)
    statuses = manager.status(profile)

    if not statuses:
        if json_output:
            console.print(json_mod.dumps({"tunnels": [], "status": "no_tunnels"}))
        else:
            console.print("[yellow]No tunnels configured[/yellow]")
        return

    if json_output:
        console.print(json_mod.dumps({"tunnels": statuses}, indent=2, default=str))
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
                console.print(line)
        except FileNotFoundError:
            console.print(f"[red]Log file not found: {path}[/red]")


# ---------------------------------------------------------------------------
# Capistrano deployment detection
# ---------------------------------------------------------------------------


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
    import json as json_mod

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
                    line = f"    {svc.icon} {svc.summary}"
                    if svc.path:
                        line += f" ({svc.path})"
                    console.print(line)
                    if svc.details:
                        for k, v in svc.details.items():
                            console.print(f"      {k}: {v}")
                _record("remote_services", f"{name}/{svc.summary}", True, str(svc.details) if json_output else "")
        except Exception as e:
            if not json_output:
                console.print(f"    [yellow]⚠ Service check interrupted: {e}[/yellow]")
            issues.append(f"Service check failed for {name}: {e}")
            _record("remote_services", name, False, str(e))

    if json_output:
        console.print(json_mod.dumps({
            "sections": results,
            "issues": issues,
            "healthy": len(issues) == 0,
        }, indent=2, default=str))
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

    console.print(f"[cyan]Pulling .env from {p.host}...[/cyan]")

    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )

    output_lines: list[str] = []

    def collect(line: str) -> None:
        output_lines.append(line)

    runner = PTYRunner()

    for env_file in p.env_files:
        remote_path = f"{p.remote_path}/{env_file}"
        cmd = ssh_args + [f"cat {safe_quote(remote_path)}"]

        exit_code = runner.run(cmd, on_output=collect, timeout=15.0)

        if exit_code == 0 and output_lines:
            content = "\n".join(output_lines)
            Path(dest).write_text(content)
            console.print(f"[green]Saved to {dest}[/green]")
            return

    console.print("[red]Failed to pull environment config[/red]")


# ---------------------------------------------------------------------------
# spyro run
# ---------------------------------------------------------------------------


@click.command()
@click.option("--all", "run_all", is_flag=True, help="Run across all profiles")
@click.option("--profile", "-p", multiple=True, help="Specific profile(s)")
@click.argument("command")
def cmd_run(run_all: bool, profile: tuple[str, ...], command: str) -> None:
    """Execute a command on remote server(s)."""
    config = load_config()

    if run_all:
        profiles = config.profile_names
    elif profile:
        profiles = list(profile)
    else:
        console.print("[red]Specify --all or --profile[/red]")
        return

    runner = PTYRunner()

    for name in profiles:
        p = config.get_profile(name)
        console.print(f"\n[bold cyan]=== {name} ===[/bold cyan]")

        ssh_args = build_ssh_args(
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
        )

        # Force PTY allocation so remote sudo can prompt for password
        if p.sudo:
            ssh_args.insert(1, "-t")

        ssh_args.append(command)

        def output_line(line: str) -> None:
            console.print(f"  {line}")

        from ..utils.keychain import prompt_for_credential

        sudo_pw = ""
        if p.sudo:
            sudo_pw = prompt_for_credential(name, p.user)

        ssh_pw = prompt_for_credential(name, p.user)

        exit_code = runner.run(
            ssh_args,
            password=ssh_pw,
            sudo_password=sudo_pw,
            on_output=output_line,
            timeout=60.0,
        )

        if exit_code != 0:
            console.print(f"  [red]Exit code: {exit_code}[/red]")


# ---------------------------------------------------------------------------
# spyro watch
# ---------------------------------------------------------------------------


@click.command()
@click.argument("src")
@click.argument("dest")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_watch(src: str, dest: str, profile: str) -> None:
    """Sync local file changes to remote server in real-time."""
    import time

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        console.print("[red]watchdog not installed. Run: pip install watchdog[/red]")
        return

    config = load_config()
    p = config.get_profile(profile)

    src_path = Path(src).resolve()
    if not src_path.exists():
        console.print(f"[red]Source path does not exist: {src}[/red]")
        return

    console.print(f"[cyan]Watching {src} -> {p.host}:{dest}[/cyan] (Ctrl+C to stop)")

    class SyncHandler(FileSystemEventHandler):
        def on_any_event(self, event: object) -> None:
            time.sleep(0.1)

            src_file = Path(event.src_path)  # type: ignore[attr-defined]
            if src_file.is_dir():
                return
            rel = src_file.relative_to(src_path)
            remote_dest = f"{p.host}:{dest}/{rel}"

            scp_args = build_scp_args(
                src=str(src_file),
                dest=remote_dest,
                host=p.host,
                user=p.user,
                port=p.port,
                key=p.key,
                recursive=False,
            )

            try:
                subprocess.run(scp_args, capture_output=True, timeout=10)
                console.print(f"  [green]Synced: {rel}[/green]")
            except Exception as e:
                console.print(f"  [red]Sync failed: {rel}: {e}[/red]")

    observer = Observer()
    observer.schedule(SyncHandler(), str(src_path), recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


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

    tunnel = get_tunnel(profile)
    local_port = port or (tunnel.local_port if tunnel else p.db.port)

    db = DatabaseConfig(
        host=p.db.host,
        port=local_port,
        name=p.db.name,
        user=p.db.user,
        password=p.db.password,
        driver=p.db.driver,
    )

    url = generate_connection_url(db)
    console.print(url)


# ---------------------------------------------------------------------------
# spyro artisan
# ---------------------------------------------------------------------------


@click.command()
@click.argument("cmd_args", nargs=-1)
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--profile", "-p", default=None, help="Profile name (auto-detects if only one exists)")
def cmd_artisan(cmd_args: tuple[str, ...], no_escalate: bool, profile: str | None) -> None:
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

    runner = PTYRunner()

    sudo_prefix = ""
    if not no_escalate and p.sudo:
        sudo_prefix = "sudo "

    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )

    cd_cmd = _capistrano_cd(p.remote_path)

    artisan_cmd = f"{cd_cmd} && {sudo_prefix}php artisan {' '.join(safe_quote(a) for a in cmd_args)}"

    # Force PTY allocation so remote sudo can prompt for password
    if p.sudo and not no_escalate:
        ssh_args.insert(1, "-t")

    ssh_args.append(artisan_cmd)

    from ..utils.keychain import prompt_for_credential

    sudo_pw = ""
    if p.sudo and not no_escalate:
        sudo_pw = prompt_for_credential(profile, p.user)

    ssh_pw = prompt_for_credential(profile, p.user)

    def output_line(line: str) -> None:
        console.print(line)

    exit_code = runner.run(
        ssh_args,
        password=ssh_pw,
        sudo_password=sudo_pw,
        on_output=output_line,
        timeout=60.0,
    )

    if exit_code != 0:
        console.print(f"\n[red]Exit code: {exit_code}[/red]")


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
    import json as json_mod

    pins = load_pins()
    if not pins:
        if json_output:
            console.print(json_mod.dumps({"pins": []}))
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
        console.print(json_mod.dumps({"pins": pin_list}, indent=2))
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


@click.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--dry-run", is_flag=True, help="Show what would be synced without uploading")
def cmd_sync(profile: str, dry_run: bool) -> None:
    """Watch pinned directories and auto-sync to remote server."""
    import time

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        console.print("[red]watchdog not installed. Run: pip install watchdog[/red]")
        return

    config = load_config()
    p = config.get_profile(profile)

    all_pins = load_pins()
    pins = [pin for pin in all_pins if pin.profile == profile]

    if not pins:
        console.print(f"[yellow]No pinned directories for profile '{profile}'[/yellow]")
        console.print("Use 'spyro pin <local> <remote> -p <profile>' to add one.")
        return

    console.print(f"[cyan]Syncing {len(pins)} pinned director(y/ies) for {profile}[/cyan]")
    for pin in pins:
        exclude_files, exclude_dirs = pin.get_all_excludes()
        console.print(f"  {pin.local_path} -> {pin.remote_path} ({len(exclude_files)} exclude patterns)")

    if dry_run:
        console.print("\n[yellow]Dry run mode -- no files will be uploaded[/yellow]\n")

    class SyncHandler(FileSystemEventHandler):
        def __init__(self) -> None:
            self._debounce: dict[str, float] = {}

        def on_any_event(self, event: object) -> None:
            if event.is_directory:  # type: ignore[attr-defined]
                return

            src = Path(event.src_path)  # type: ignore[attr-defined]

            matched_pin = None
            for pin in pins:
                local = Path(pin.local_path)
                try:
                    src.relative_to(local)
                    matched_pin = pin
                    break
                except ValueError:
                    continue

            if not matched_pin:
                return

            now = time.time()
            key = str(src)
            if key in self._debounce and now - self._debounce[key] < 0.1:
                return
            self._debounce[key] = now

            local_base = Path(matched_pin.local_path)
            exclude_files, exclude_dirs = matched_pin.get_all_excludes()

            if should_exclude(src, local_base, exclude_files, exclude_dirs, matched_pin.include_patterns):
                if dry_run:
                    console.print(f"  [dim]Skipped (excluded): {src.relative_to(local_base)}[/dim]")
                return

            rel = src.relative_to(local_base)
            remote_dest = f"{p.host}:{matched_pin.remote_path}/{rel}"

            if dry_run:
                console.print(f"  [green]Would sync: {rel}[/green]")
                return

            scp_args = build_scp_args(src=str(src), dest=remote_dest, host=p.host, user=p.user, port=p.port, key=p.key, recursive=False)

            try:
                proc = subprocess.run(scp_args, capture_output=True, timeout=15)
                if proc.returncode == 0:
                    console.print(f"  [green]Synced: {rel}[/green]")
                else:
                    console.print(f"  [red]Failed: {rel}[/red]")
            except Exception as e:
                console.print(f"  [red]Error syncing {rel}: {e}[/red]")

    observer = Observer()
    for pin in pins:
        local = Path(pin.local_path)
        if local.exists():
            observer.schedule(SyncHandler(), str(local), recursive=True)
            console.print(f"  [dim]Watching: {local}[/dim]")

    observer.start()
    console.print("\n[cyan]Syncing... (Ctrl+C to stop)[/cyan]\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


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

    runner = PTYRunner()

    # Find WP-CLI on remote
    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )
    wp_bin = _find_wp_cli(ssh_args, p.wp_cli_path)

    cd_cmd = _capistrano_cd(p.remote_path)

    # Build command
    sudo_prefix = ""
    if not no_escalate and p.sudo:
        sudo_prefix = "sudo "

    wp_cmd = f"{cd_cmd} && {sudo_prefix}{wp_bin} {' '.join(safe_quote(a) for a in cmd_args)}"

    ssh_args.append(wp_cmd)

    # Force PTY allocation so remote sudo can prompt for password
    if p.sudo and not no_escalate:
        ssh_args.insert(1, "-t")

    from ..utils.keychain import prompt_for_credential

    sudo_pw = ""
    if p.sudo and not no_escalate:
        sudo_pw = prompt_for_credential(profile, p.user)

    ssh_pw = prompt_for_credential(profile, p.user)

    def output_line(line: str) -> None:
        console.print(line)

    exit_code = runner.run(
        ssh_args,
        password=ssh_pw,
        sudo_password=sudo_pw,
        on_output=output_line,
        timeout=60.0,
    )

    if exit_code != 0:
        console.print(f"\n[red]Exit code: {exit_code}[/red]")


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


def _copy_to_profile(src: str, dest: str, recursive: bool, profile_name: str) -> int:
    """Copy files to/from a single profile. Returns exit code."""
    config = load_config()
    p = config.get_profile(profile_name)
    runner = PTYRunner()

    src_is_local = _is_local_path(src)

    from ..core.pty_engine import _scp_target

    if src_is_local:
        # Local -> remote (dest is on the remote host via profile)
        resolved_src = str(Path(src).expanduser().resolve())
        scp_args = build_scp_args(
            src=resolved_src,
            dest=_scp_target(dest, p.host, p.user),
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

    console.print(f"[cyan][{profile_name}] Copying {src} -> {dest}...[/cyan]")

    from ..utils.keychain import prompt_for_credential

    ssh_pw = prompt_for_credential(profile_name, p.user)

    def output_line(line: str) -> None:
        console.print(f"  [{profile_name}] {line}")

    exit_code = runner.run(
        scp_args,
        password=ssh_pw,
        on_output=output_line,
        timeout=120.0,
    )

    if exit_code == 0:
        console.print(f"[green]  [{profile_name}] Copy complete[/green]")
    else:
        console.print(f"[red]  [{profile_name}] Copy failed (exit code: {exit_code})[/red]")

    return exit_code


@click.command()
@click.argument("src")
@click.argument("dest")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories")
@click.option("--profile", "-p", multiple=True, default=None, help="Profile name (can be used multiple times)")
@click.option("--all", "all_profiles", is_flag=True, help="Copy to all profiles")
@click.option("--except", "except_profiles", default="", help="Comma-separated profiles to exclude when using --all")
def cmd_cp(src: str, dest: str, recursive: bool, profile: tuple[str, ...] | None, all_profiles: bool, except_profiles: str) -> None:
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
        console.print("[red]Specify at least one --profile/-p or --all[/red]")
        return

    if not targets:
        console.print("[red]No profiles matched[/red]")
        return

    console.print(f"[bold cyan]Copying to {len(targets)} profile(s): {', '.join(targets)}[/bold cyan]\n")

    results: dict[str, int] = {}
    for name in targets:
        ec = _copy_to_profile(src, dest, recursive, name)
        results[name] = ec

    # Summary
    successes = [n for n, ec in results.items() if ec == 0]
    failures = [n for n, ec in results.items() if ec != 0]
    if successes:
        console.print(f"\n[green]✓ Succeeded: {len(successes)} profile(s)[/green]")
    if failures:
        console.print(f"\n[red]✗ Failed: {len(failures)} profile(s) — {', '.join(failures)}[/red]")


# ---------------------------------------------------------------------------
# spyro db-tunnel
# ---------------------------------------------------------------------------


@click.command(name="db-tunnel")
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_db_tunnel(port: int | None, profile: str) -> None:
    """Manage tunnel and print connection status."""
    config = load_config()
    manager = TunnelManager(config)

    tunnel = get_tunnel(profile)
    if not tunnel or tunnel.status != "running":
        console.print(f"[cyan]Starting tunnel for {profile}...[/cyan]")
        tunnel = manager.start(profile)

    local_port = port or tunnel.local_port
    p = config.get_profile(profile)

    db_url = generate_connection_url(p.db, port_override=local_port)
    console.print(f"\n[bold green]Database tunnel active[/bold green]")
    console.print(f"  Profile:   {profile}")
    console.print(f"  Local:     127.0.0.1:{local_port}")
    console.print(f"  Remote:    {p.host}:{p.db.port}")
    console.print(f"  URL:       {db_url}")


# ---------------------------------------------------------------------------
# spyro db-shell
# ---------------------------------------------------------------------------


@click.command(name="db-shell")
@click.option("--no-tunnel", is_flag=True, help="Skip tunnel management")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_db_shell(no_tunnel: bool, profile: str) -> None:
    """Launch pre-authenticated local database CLI."""
    config = load_config()
    p = config.get_profile(profile)

    if not no_tunnel:
        manager = TunnelManager(config)
        tunnel = get_tunnel(profile)
        if not tunnel or tunnel.status != "running":
            console.print(f"[cyan]Starting tunnel for {profile}...[/cyan]")
            tunnel = manager.start(profile)

        local_port = tunnel.local_port
    else:
        local_port = p.db.port

    if p.db.driver == "mysql":
        client = "mysql"
        args = [
            client,
            f"-h127.0.0.1",
            f"-P{local_port}",
            f"-u{p.db.user}",
        ]
        if p.db.password:
            args.append(f"-p{p.db.password}")
        args.append(p.db.name)
    elif p.db.driver == "postgres":
        client = "psql"
        env = os.environ.copy()
        env["PGHOST"] = "127.0.0.1"
        env["PGPORT"] = str(local_port)
        env["PGUSER"] = p.db.user
        env["PGDATABASE"] = p.db.name
        if p.db.password:
            env["PGPASSWORD"] = p.db.password
        args = [client]
    else:
        console.print(f"[red]Unsupported driver: {p.db.driver}[/red]")
        return

    console.print(f"[cyan]Connecting to {p.db.name} via {client}...[/cyan]")
    try:
        if p.db.driver == "postgres":
            os.execvpe(client, args, env)
        else:
            os.execvp(client, args)
    except FileNotFoundError:
        console.print(f"[red]Database client '{client}' not found[/red]")


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


@cmd_auth.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--password", "-w", default="", help="Password (omit to prompt)")
@click.option("--force", "-f", is_flag=True, help="Overwrite without prompting")
def set(profile: str, password: str, force: bool) -> None:
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
                masked = pw[:2] + "••••" + pw[-2:] if len(pw) > 4 else "••••"
                console.print(f"  [green]✓[/green] {name}: {username} / {masked}")
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


def _run_svc_cmd(profile: str, cmd: str, timeout: float = 30.0) -> int:
    """Run a command via SSH with PTY auth for a profile.

    Returns the exit code.
    """
    config = load_config()
    p = config.get_profile(profile)
    runner = PTYRunner()

    # Check if command requires sudo but profile doesn't have sudo access
    if not p.sudo and "sudo" in cmd:
        console.print(f"[red]  ✗ User '{p.user}' does not have sudo access on {profile}[/red]")
        console.print(f"[yellow]  Set sudo = true in your spyro.toml for this profile[/yellow]")
        return 1

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    if p.sudo:
        ssh_args.insert(1, "-t")
    ssh_args.append(cmd)

    from ..utils.keychain import prompt_for_credential

    sudo_pw = prompt_for_credential(profile, p.user) if p.sudo else ""
    ssh_pw = prompt_for_credential(profile, p.user)

    def output_line(line: str) -> None:
        console.print(f"  {line}")

    return runner.run(ssh_args, password=ssh_pw, sudo_password=sudo_pw,
                      on_output=output_line, timeout=timeout)


@click.group()
def cmd_supervisor() -> None:
    """Manage Supervisor processes on remote server."""


@cmd_supervisor.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Supervisor process status."""
    ec = _run_svc_cmd(profile, "sudo supervisorctl status")
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_supervisor.command()
@click.argument("process", default="all")
@click.option("--profile", "-p", required=True, help="Profile name")
def restart(profile: str, process: str) -> None:
    """Restart Supervisor process(es). Default: all."""
    ec = _run_svc_cmd(profile, f"sudo supervisorctl restart {process}", timeout=60)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
def start(profile: str, process: str) -> None:
    """Start a Supervisor process."""
    ec = _run_svc_cmd(profile, f"sudo supervisorctl start {process}")
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
def stop(profile: str, process: str) -> None:
    """Stop a Supervisor process."""
    ec = _run_svc_cmd(profile, f"sudo supervisorctl stop {process}")
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_supervisor.command()
@click.argument("process")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines to tail")
def tail(profile: str, process: str, lines: int) -> None:
    """Tail Supervisor process stderr log."""
    ec = _run_svc_cmd(profile, f"sudo supervisorctl tail -{lines} {process} 2>/dev/null || sudo supervisorctl tail {process}")
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    ec = _run_svc_cmd(profile, "redis-cli ping", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_redis.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--section", "-s", default="", help="Info section (server, stats, keyspace, etc.)")
def info(profile: str, section: str) -> None:
    """Show Redis server info."""
    cmd = f"redis-cli info {section}".strip()
    ec = _run_svc_cmd(profile, cmd, timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_redis.command()
@click.argument("command", nargs=-1, required=True)
@click.option("--profile", "-p", required=True, help="Profile name")
def cli(profile: str, command: tuple[str, ...]) -> None:
    """Run an arbitrary redis-cli command."""
    cmd_str = " ".join(safe_quote(a) for a in command)
    ec = _run_svc_cmd(profile, f"redis-cli {cmd_str}", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_redis.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def stats(profile: str) -> None:
    """Show Redis key metrics (connections, commands, keyspace)."""
    ec = _run_svc_cmd(profile, 'redis-cli info stats | grep -E "^(total_connections|total_commands|keyspace_|instantaneous)"', timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    ec = _run_svc_cmd(profile, "php -v 2>/dev/null | head -3", timeout=10)
    if ec != 0:
        ec = _run_svc_cmd(profile, "php --version 2>/dev/null | head -3", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def fpm_status(profile: str) -> None:
    """Show PHP-FPM status (pools, processes)."""
    ec = _run_svc_cmd(profile, 'php-fpm -tt 2>/dev/null || (echo "PHP-FPM config test:" && pgrep -af "php-fpm" 2>/dev/null || echo "not running")', timeout=10)
    if ec != 0:
        ec = _run_svc_cmd(profile, "pgrep -af 'php-fpm' 2>/dev/null || echo 'PHP-FPM not running'", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--filter", "-f", default="", help="Filter extensions (grep pattern)")
def extensions(profile: str, filter: str) -> None:
    """List PHP extensions."""
    cmd = "php -m 2>/dev/null | tail -n +2"
    if filter:
        cmd += f" | grep -i {safe_quote(filter)}"
    ec = _run_svc_cmd(profile, cmd, timeout=10)
    if ec != 0:
        ec = _run_svc_cmd(profile, "php -m 2>/dev/null || echo 'PHP not available'", timeout=10)


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--option", "-o", default="", help="Specific ini option (e.g. memory_limit)")
def info(profile: str, option: str) -> None:
    """Show PHP configuration."""
    cmd = "php -i 2>/dev/null"
    if option:
        cmd += f" | grep -i {safe_quote(option)}"
    ec = _run_svc_cmd(profile, cmd, timeout=15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_php.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def restart(profile: str) -> None:
    """Restart PHP-FPM."""
    ec = _run_svc_cmd(profile, "sudo systemctl restart php*-fpm 2>/dev/null || sudo service php*-fpm restart 2>/dev/null || (echo 'Trying sudo kill -USR2...' && sudo kill -USR2 $(pgrep -f 'php-fpm: master' | head -1) 2>/dev/null || echo 'Could not restart PHP-FPM')", timeout=30)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    ec = _run_svc_cmd(profile, "apache2 -v 2>/dev/null || httpd -v 2>/dev/null || echo 'Apache not found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def modules(profile: str) -> None:
    """List loaded Apache modules."""
    ec = _run_svc_cmd(profile, "apache2 -M 2>/dev/null || httpd -M 2>/dev/null || echo 'Apache not found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Apache server status."""
    ec = _run_svc_cmd(profile, "apache2ctl status 2>/dev/null || apachectl status 2>/dev/null || (pgrep -x apache2 >/dev/null && echo 'Apache running' || echo 'Apache not running')", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def sites(profile: str) -> None:
    """List enabled Apache virtual hosts."""
    ec = _run_svc_cmd(profile, "ls -1 /etc/apache2/sites-enabled/ 2>/dev/null || ls -1 /etc/httpd/sites-enabled/ 2>/dev/null || echo 'No sites-enabled found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_apache.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def restart(profile: str) -> None:
    """Restart Apache."""
    ec = _run_svc_cmd(profile, "sudo systemctl restart apache2 2>/dev/null || sudo systemctl restart httpd 2>/dev/null || sudo service apache2 restart 2>/dev/null || sudo service httpd restart 2>/dev/null || echo 'Could not restart Apache'", timeout=30)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    ec = _run_svc_cmd(profile, "nginx -v 2>&1 || echo 'Nginx not found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Nginx server status."""
    ec = _run_svc_cmd(profile, "nginx -t 2>&1 && (pgrep -x nginx >/dev/null && echo 'Nginx running' || echo 'Nginx not running')", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def sites(profile: str) -> None:
    """List enabled Nginx site configs."""
    ec = _run_svc_cmd(profile, "ls -1 /etc/nginx/sites-enabled/ 2>/dev/null || ls -1 /etc/nginx/conf.d/ 2>/dev/null || echo 'No site configs found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_nginx.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def restart(profile: str) -> None:
    """Restart Nginx."""
    ec = _run_svc_cmd(profile, "sudo systemctl restart nginx 2>/dev/null || sudo service nginx restart 2>/dev/null || sudo nginx -s reload 2>/dev/null || echo 'Could not restart Nginx'", timeout=30)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    ec = _run_svc_cmd(profile, "caddy version 2>&1 || echo 'Caddy not found'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_caddy.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def status(profile: str) -> None:
    """Show Caddy server status."""
    ec = _run_svc_cmd(profile, "(pgrep -x caddy >/dev/null || pgrep -f 'caddy run' >/dev/null) && (caddy version 2>/dev/null || echo 'running') || echo 'Caddy not running'", timeout=10)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_caddy.command()
@click.option("--profile", "-p", required=True, help="Profile name")
def restart(profile: str) -> None:
    """Restart Caddy."""
    ec = _run_svc_cmd(profile, "sudo systemctl restart caddy 2>/dev/null || sudo service caddy restart 2>/dev/null || (sudo kill -USR1 $(pgrep -x caddy | head -1) 2>/dev/null && echo 'Sent reload signal') || echo 'Could not restart Caddy'", timeout=30)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


# ---------------------------------------------------------------------------
# spyro tinker — Laravel Tinker REPL
# ---------------------------------------------------------------------------


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

    runner = PTYRunner()

    sudo_prefix = ""
    if not no_escalate and p.sudo:
        sudo_prefix = "sudo "

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)

    cd_cmd = _capistrano_cd(p.remote_path)

    if eval:
        tinker_cmd = f"{cd_cmd} && {sudo_prefix}php artisan tinker --execute={safe_quote(eval)}"
    elif file:
        tmp_path = f"/tmp/spyro-tinker-{os.path.basename(file)}"
        console.print(f"[cyan]Uploading {file} to {p.host}:{tmp_path}...[/cyan]")
        from ..core.pty_engine import _scp_target
        scp_args = build_scp_args(
            src=file,
            dest=_scp_target(tmp_path, p.host, p.user),
            host=p.host, user=p.user, port=p.port, key=p.key,
        )
        from ..utils.keychain import prompt_for_credential as pfc
        ssh_pw = pfc(profile, p.user) if not no_escalate else ""
        runner.run(scp_args, password=ssh_pw, timeout=30)
        tinker_cmd = f"{cd_cmd} && {sudo_prefix}php artisan tinker < {tmp_path}; {sudo_prefix}rm -f {tmp_path}"
    else:
        tinker_cmd = f"{cd_cmd} && {sudo_prefix}php artisan tinker"

    # Force PTY allocation
    # - sudo profiles already got -t above
    # - non-sudo profiles need -t here (used by both interactive and eval/file)
    if not no_escalate and not p.sudo:
        ssh_args.insert(1, "-t")

    ssh_args.append(tinker_cmd)

    from ..utils.keychain import prompt_for_credential as pfc2

    sudo_pw = pfc2(profile, p.user) if p.sudo and not no_escalate else ""
    ssh_pw = pfc2(profile, p.user)

    if eval or file:
        def output_line(line: str) -> None:
            console.print(line)
        exit_code = runner.run(
            ssh_args, password=ssh_pw, sudo_password=sudo_pw,
            on_output=output_line, timeout=120,
        )
        if exit_code != 0:
            console.print(f"  [red]Exit code: {exit_code}[/red]")
    else:
        console.print(f"[cyan]Starting Tinker on {profile}...[/cyan]")
        console.print("[dim]Exit with Ctrl+D or type 'exit'[/dim]")
        exit_code = runner.interactive_run(
            ssh_args, password=ssh_pw, sudo_password=sudo_pw, timeout=3600,
        )


# ---------------------------------------------------------------------------
# spyro eval — Evaluate PHP expression on remote Laravel
# ---------------------------------------------------------------------------


@click.command()
@click.argument("expression")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-escalate", is_flag=True, help="Don't use sudo")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_eval(expression: str, json_output: bool, no_escalate: bool, profile: str) -> None:
    """Evaluate a PHP expression on the remote Laravel server.

    Boots Laravel via bootstrap/app.php and evaluates the expression directly
    with php -r (no PsySH). Outputs the result reliably. Avoids the quoting
    and output-capture issues of `spyro tinker -e`.

    Examples:

      spyro eval 'User::count()' -p staging

      spyro eval \"\\App\\Models\\User::first()->toArray()\" -p staging

      spyro eval 'DB::table(\"users\")->count()' -p staging --json
    """
    config = load_config()
    p = config.get_profile(profile)

    if not p.artisan:
        console.print(f"[yellow]Profile '{profile}' is not configured for artisan[/yellow]")
        return

    if not p.remote_path:
        console.print(f"[red]Profile '{profile}' has no remote_path configured[/red]")
        return

    # Build PHP code that boots Laravel and evaluates the expression
    # Uses getcwd() instead of __DIR__ so the file can live in /tmp/ while
    # we cd into the project directory.
    if json_output:
        output_expr = (
            f"json_encode((function() {{ return {expression}; }})(),"
            " JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE)"
        )
    else:
        output_expr = f"print_r((function() {{ return {expression}; }})(), true)"

    php_code = (
        "<?php\n"
        "$base = getcwd();\n"
        "require $base . '/vendor/autoload.php';\n"
        "$app = require_once $base . '/bootstrap/app.php';\n"
        "$kernel = $app->make(Illuminate\\Contracts\\Console\\Kernel::class);\n"
        "$kernel->bootstrap();\n"
        "echo " + output_expr + ";\n"
        "echo \"\\n\";\n"
    )

    # Write temp file locally
    import uuid
    import tempfile
    from pathlib import Path

    tmp_name = f"spyro-eval-{uuid.uuid4().hex[:8]}.php"
    tmp_local = Path(tempfile.gettempdir()) / tmp_name
    tmp_local.write_text(php_code)

    runner = PTYRunner()

    try:
        # Upload to remote /tmp/
        from ..core.pty_engine import _scp_target

        tmp_remote = f"/tmp/{tmp_name}"
        scp_args = build_scp_args(
            src=str(tmp_local),
            dest=_scp_target(tmp_remote, p.host, p.user),
            host=p.host, user=p.user, port=p.port, key=p.key,
        )

        from ..utils.keychain import prompt_for_credential

        ssh_pw = prompt_for_credential(profile, p.user)

        console.print(f"[cyan]Uploading eval to {p.host}:{tmp_remote}...[/cyan]")
        ec = runner.run(scp_args, password=ssh_pw, timeout=30)
        if ec != 0:
            console.print("[red]Failed to upload eval script[/red]")
            return

        # Run it
        cd_cmd = _capistrano_cd(p.remote_path)
        sudo_prefix = "sudo " if not no_escalate and p.sudo else ""
        run_cmd = f"{cd_cmd} && {sudo_prefix}php {tmp_remote}"

        ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
        if not no_escalate and p.sudo:
            ssh_args.insert(1, "-t")
        ssh_args.append(run_cmd)

        sudo_pw = prompt_for_credential(profile, p.user) if p.sudo and not no_escalate else ""

        def output_line(line: str) -> None:
            console.print(line)

        exit_code = runner.run(
            ssh_args, password=ssh_pw, sudo_password=sudo_pw,
            on_output=output_line, timeout=120,
        )

        # Cleanup remote
        clean_cmd = f"rm -f {safe_quote(tmp_remote)}"
        clean_ssh = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
        clean_ssh.append(clean_cmd)
        runner.run(clean_ssh, password=ssh_pw, timeout=10)

        if exit_code != 0:
            console.print(f"  [red]Exit code: {exit_code}[/red]")
    finally:
        # Cleanup local
        try:
            tmp_local.unlink()
        except Exception:
            pass


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
    config = load_config()
    p = config.get_profile(profile)

    manager = TunnelManager(config)
    tunnel = get_tunnel(profile)
    if not tunnel or tunnel.status != "running":
        tunnel = manager.start(profile)
    local_port = tunnel_port or (tunnel.local_port if tunnel else p.db.port)

    client = _detect_db_client(profile)
    if client in ("mysql", "mariadb"):
        pw_flag = f"-p{p.db.password}" if p.db.password else "--skip-password"
        cmd_list = [client, f"-h127.0.0.1", f"-P{local_port}", f"-u{p.db.user}",
                    "--skip-ssl", pw_flag, p.db.name, "-e", query]
    elif client == "psql":
        env = os.environ.copy()
        env.update({"PGHOST": "127.0.0.1", "PGPORT": str(local_port), "PGUSER": p.db.user, "PGDATABASE": p.db.name})
        if p.db.password:
            env["PGPASSWORD"] = p.db.password
        cmd_list = [client, "-c", query]
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=10, env=env)
        return result.returncode, result.stdout + result.stderr
    else:
        return 1, f"Unknown client: {client}"

    try:
        result = subprocess.run(cmd_list, capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout
    except FileNotFoundError:
        return 1, f"Client not found. Install mysql-client, mariadb-client, or postgresql-client."
    except subprocess.TimeoutExpired:
        return 1, "Query timed out"


@click.group(invoke_without_command=True)
@click.pass_context
def cmd_db(ctx: click.Context) -> None:
    """Database commands (MySQL/MariaDB/PostgreSQL).

    If no subcommand is given, starts a tunnel and shows connection info.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(tunnel)


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def tunnel(port: int | None, profile: str) -> None:
    """Start tunnel and print connection URL."""
    config = load_config()
    manager = TunnelManager(config)
    tunnel_state = get_tunnel(profile)
    if not tunnel_state or tunnel_state.status != "running":
        console.print(f"[cyan]Starting tunnel for {profile}...[/cyan]")
        tunnel_state = manager.start(profile)
    local_port = port or (tunnel_state.local_port if tunnel_state else 3306)
    p = config.get_profile(profile)
    db_url = generate_connection_url(p.db, port_override=local_port)
    console.print(f"\n[bold green]Database tunnel active[/bold green]")
    console.print(f"  Profile:   {profile}")
    console.print(f"  Local:     127.0.0.1:{local_port}")
    console.print(f"  Remote:    {p.host}:{p.db.port}")
    console.print(f"  URL:       {db_url}")


@cmd_db.command()
@click.option("--no-tunnel", is_flag=True, help="Skip tunnel management")
@click.option("--profile", "-p", required=True, help="Profile name")
def shell(no_tunnel: bool, profile: str) -> None:
    """Launch pre-authenticated database CLI (mysql/mariadb/psql)."""
    config = load_config()
    p = config.get_profile(profile)
    if not no_tunnel:
        manager = TunnelManager(config)
        tunnel_state = get_tunnel(profile)
        if not tunnel_state or tunnel_state.status != "running":
            console.print(f"[cyan]Starting tunnel for {profile}...[/cyan]")
            tunnel_state = manager.start(profile)
        local_port = tunnel_state.local_port
    else:
        local_port = p.db.port
    client = _detect_db_client(profile)
    if client in ("mysql", "mariadb"):
        args = [client, f"-h127.0.0.1", f"-P{local_port}", f"-u{p.db.user}", "--skip-ssl"]
        args.append(f"-p{p.db.password}" if p.db.password else "--skip-password")
        args.append(p.db.name)
        env = None
    elif client == "psql":
        env = os.environ.copy()
        env.update({"PGHOST": "127.0.0.1", "PGPORT": str(local_port), "PGUSER": p.db.user, "PGDATABASE": p.db.name})
        if p.db.password:
            env["PGPASSWORD"] = p.db.password
        args = [client]
    else:
        console.print("[red]No database client found. Install mysql, mariadb, or psql.[/red]")
        return
    console.print(f"[cyan]Connecting to {p.db.name} via {client}...[/cyan]")
    try:
        if env:
            os.execvpe(client, args, env)
        else:
            os.execvp(client, args)
    except FileNotFoundError:
        console.print(f"[red]'{client}' not found locally[/red]")


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def ping(port: int | None, profile: str) -> None:
    """Test database connectivity through the tunnel."""
    config = load_config()
    p = config.get_profile(profile)
    manager = TunnelManager(config)
    tunnel_state = get_tunnel(profile)
    if not tunnel_state or tunnel_state.status != "running":
        console.print(f"[cyan]Starting tunnel for {profile}...[/cyan]")
        tunnel_state = manager.start(profile)
    local_port = port or (tunnel_state.local_port if tunnel_state else p.db.port)
    client = _detect_db_client(profile)
    console.print(f"[cyan]Pinging {p.db.driver}@{p.host}:{p.db.port} via 127.0.0.1:{local_port}...[/cyan]")
    if client in ("mysql", "mariadb"):
        cmd = ["mysqladmin", f"-h127.0.0.1", f"-P{local_port}", f"-u{p.db.user}",
               "--skip-ssl"]
        if p.db.password:
            cmd.append(f"-p{p.db.password}")
        cmd.extend(["ping", "--silent"])
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10)
            stdout = result.stdout.decode()
            stderr = result.stderr.decode()
            # mysqladmin returns exit code 0 on success even with SSL warnings
            if result.returncode == 0 or "mysqld is alive" in stdout:
                console.print("[green]✓ mysqld is alive[/green]")
            elif "Access denied" in stderr:
                console.print("[red]✗ Access denied[/red]")
            else:
                console.print(f"[red]✗ Ping failed[/red]")
                if stderr:
                    console.print(f"  {stderr.strip()}")
        except FileNotFoundError:
            console.print("[red]mysqladmin not found locally[/red]")
    elif client == "psql":
        env = os.environ.copy()
        env.update({"PGHOST": "127.0.0.1", "PGPORT": str(local_port), "PGUSER": p.db.user, "PGDATABASE": p.db.name or "postgres"})
        if p.db.password:
            env["PGPASSWORD"] = p.db.password
        try:
            result = subprocess.run(["psql", "-c", "SELECT 1"], capture_output=True, timeout=10, env=env)
            if result.returncode == 0:
                console.print("[green]✓ PONG[/green]")
            else:
                console.print(f"[red]✗ {result.stderr.decode()[:200]}[/red]")
        except FileNotFoundError:
            console.print("[red]psql not found locally[/red]")


@cmd_db.command()
@click.argument("query")
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def query(profile: str, query: str, port: int | None) -> None:
    """Run a SQL query through the tunnel."""
    ec, output = _run_db_query(profile, query, tunnel_port=port)
    if ec == 0:
        console.print(output)
    else:
        console.print(f"[red]{output}[/red]")


@cmd_db.command()
@click.option("--port", type=int, help="Override local port")
@click.option("--profile", "-p", required=True, help="Profile name")
def list_databases(port: int | None, profile: str) -> None:
    """List databases on the remote server."""
    client = _detect_db_client(profile)
    if client in ("mysql", "mariadb"):
        ec, output = _run_db_query(profile, "SHOW DATABASES", tunnel_port=port)
    elif client == "psql":
        ec, output = _run_db_query(profile, "\\l", tunnel_port=port)
    else:
        console.print("[red]Unknown client[/red]")
        return
    if ec == 0:
        console.print(output)
    else:
        console.print(f"[red]{output}[/red]")


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
    log_path = f"{p.remote_path}/storage/logs/laravel.log"
    tail_flag = " -f" if follow else ""
    ec = _run_svc_cmd(profile, f"tail -n {lines}{tail_flag} {log_path} 2>/dev/null || echo 'Log not found'", timeout=60 if follow else 15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def nginx(profile: str, lines: int, follow: bool) -> None:
    """Tail Nginx access log."""
    tail_flag = " -f" if follow else ""
    ec = _run_svc_cmd(profile, f"tail -n {lines}{tail_flag} /var/log/nginx/access.log 2>/dev/null || tail -n {lines}{tail_flag} /var/log/nginx/*access* 2>/dev/null || echo 'Nginx access log not found'", timeout=60 if follow else 15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_logs.command(name="nginx-error")
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def nginx_error(profile: str, lines: int, follow: bool) -> None:
    """Tail Nginx error log."""
    tail_flag = " -f" if follow else ""
    ec = _run_svc_cmd(profile, f"tail -n {lines}{tail_flag} /var/log/nginx/error.log 2>/dev/null || echo 'Nginx error log not found'", timeout=60 if follow else 15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def apache(profile: str, lines: int, follow: bool) -> None:
    """Tail Apache access log."""
    tail_flag = " -f" if follow else ""
    ec = _run_svc_cmd(profile, f"tail -n {lines}{tail_flag} /var/log/apache2/access.log 2>/dev/null || tail -n {lines}{tail_flag} /var/log/httpd/access_log 2>/dev/null || echo 'Apache access log not found'", timeout=60 if follow else 15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


@cmd_logs.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--lines", "-n", default=50, help="Number of lines")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def php(profile: str, lines: int, follow: bool) -> None:
    """Tail PHP-FPM error log."""
    tail_flag = " -f" if follow else ""
    ec = _run_svc_cmd(profile, f"tail -n {lines}{tail_flag} /var/log/php*-fpm.log 2>/dev/null || tail -n {lines}{tail_flag} /var/log/php*.log 2>/dev/null || echo 'PHP-FPM log not found'", timeout=60 if follow else 15)
    if ec != 0:
        console.print(f"  [red]Exit code: {ec}[/red]")


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
    config = load_config()
    p = config.get_profile(profile)
    table_list = [t.strip() for t in tables.split(",") if t.strip()] if tables else []

    if p.db.driver not in ("mysql", "mariadb"):
        console.print(f"[red]Dump not yet supported for driver: {p.db.driver}[/red]")
        return

    # Build remote mysqldump command — run through SSH on remote server
    # This avoids tunnel port mapping issues and uses the remote mysqldump
    dump_cmd_parts = [
        "mysqldump",
        f"-h{p.db.host}",
        f"-P3306",  # Remote MySQL always on 3306 for remote execution
        f"-u{p.db.user}",
        f"-p{p.db.password}" if p.db.password else "--skip-password",
        "--single-transaction", "--quick", "--no-tablespaces",
        "--routines", "--triggers",
    ]
    if no_data:
        dump_cmd_parts.append("--no-data")
    if where:
        dump_cmd_parts += ["--where", where]
    dump_cmd_parts.append(p.db.name)
    dump_cmd_parts += table_list

    # Quote args for remote shell
    import shlex
    quoted = []
    for a in dump_cmd_parts:
        if not a.startswith("-"):
            quoted.append(a)
        elif a.startswith("-p") and len(a) > 2:
            # Password arg: use -p with quoted password
            quoted.append(f"-p{shlex.quote(a[2:])}")
        else:
            quoted.append(shlex.quote(a))
    ssh_cmd = " ".join(quoted)
    if gzip:
        ssh_cmd += " | gzip -c"

    # Determine output path
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if not output:
        suffix = ".sql.gz" if gzip else ".sql"
        tbl_suffix = f"-{tables.replace(',', '-')}" if tables else ""
        output_path = Path.cwd() / f"{profile}-{p.db.name}{tbl_suffix}-{ts}{suffix}"
    else:
        output_path = Path(output).expanduser().resolve()
        if gzip and not str(output_path).endswith(".gz"):
            output_path = Path(str(output_path) + ".gz")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    console.print(f"[cyan]Dumping '{p.db.name}' from {p.host}...[/cyan]")
    if table_list:
        console.print(f"  Tables: {', '.join(table_list)}")
    if where:
        console.print(f"  WHERE: {where}")
    console.print(f"  Output: {output_path}")

    # Run mysqldump on remote server via SSH, pipe output to local file
    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    if p.sudo:
        ssh_args.insert(1, "-t")
    ssh_args.append(ssh_cmd)

    dump_buf: list[str] = []

    def capture(line: str) -> None:
        dump_buf.append(line)

    from ..utils.keychain import prompt_for_credential

    ssh_pw = prompt_for_credential(profile, p.user)
    sudo_pw = prompt_for_credential(profile, p.user) if p.sudo else ""

    runner = PTYRunner()
    ec = runner.run(ssh_args, password=ssh_pw, sudo_password=sudo_pw,
                    on_output=capture, timeout=600)

    if ec != 0:
        console.print(f"[red]Dump failed (exit code: {ec})[/red]")
        return

    raw = "\n".join(dump_buf)

    if gzip:
        # Write gzipped directly
        import gzip as gz
        with gz.open(str(output_path), "wt", encoding="utf-8") as f:
            f.write(raw)
    else:
        output_path.write_text(raw, encoding="utf-8")

    size = output_path.stat().st_size
    size_str = f"{size/1024:.1f} KB" if size < 1024**2 else f"{size/1024**2:.1f} MB"
    # Count lines (approximate)
    line_count = 0
    try:
        with open(output_path, "rb") as f:
            for _ in f:
                line_count += 1
    except Exception:
        line_count = 0
    console.print(f"[green]✓ Dump complete[/green]")
    console.print(f"  File:  {output_path}")
    console.print(f"  Size:  {size_str}")
    console.print(f"  Lines: ~{line_count}")


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
    ``git`` is not required — the update uses the GitHub REST API.
    """
    import json
    import urllib.request

    from .. import __version__ as current_version

    console.print("[bold cyan]Spyro Update[/bold cyan]\n")
    console.print(f"  Installed: v{current_version}\n")

    GITHUB_API = "https://api.github.com/repos/peterson-umoke/spyro-cli"
    GIT_BASE = "git+https://github.com/peterson-umoke/spyro-cli"

    # ── Fetch latest tag from GitHub API ──────────────────────────────────
    console.print("[cyan]Checking for updates...[/cyan]")
    try:
        req = urllib.request.Request(
            f"{GITHUB_API}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "spyro-cli"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
        latest_tag = release.get("tag_name", "").lstrip("v")
        if not latest_tag:
            console.print("[red]Could not parse latest release tag from GitHub[/red]")
            return
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # No releases yet — fall back to listing tags
            try:
                tag_req = urllib.request.Request(
                    f"{GITHUB_API}/tags?per_page=10",
                    headers={"Accept": "application/vnd.github+json", "User-Agent": "spyro-cli"},
                )
                with urllib.request.urlopen(tag_req, timeout=15) as resp:
                    tags = json.loads(resp.read().decode())
                if not tags:
                    console.print("[yellow]No tags found on GitHub repository[/yellow]")
                    return
                # Tags come as [{name: "v0.5.0", ...}, ...]; sort semver
                versions = []
                for t in tags:
                    raw = t.get("name", "").lstrip("v")
                    parts = raw.split(".")
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        versions.append(raw)
                if not versions:
                    console.print("[yellow]No semver tags found on GitHub[/yellow]")
                    return
                versions.sort(key=lambda v: tuple(int(x) for x in v.split(".")))
                latest_tag = versions[-1]
            except Exception as tag_err:
                console.print(f"[red]Failed to fetch tags: {tag_err}[/red]")
                return
        else:
            console.print(f"[red]GitHub API error: {e.code} {e.reason}[/red]")
            return
    except urllib.error.URLError as e:
        console.print(f"[red]Network error: {e.reason}[/red]")
        return
    except json.JSONDecodeError:
        console.print("[red]Invalid response from GitHub API[/red]")
        return

    console.print(f"  Remote latest: v{latest_tag}")

    # ── Compare versions ──────────────────────────────────────────────────
    current_parts = tuple(int(x) for x in current_version.split("."))
    latest_parts = tuple(int(x) for x in latest_tag.split("."))

    needs_update = current_parts < latest_parts

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

    # Pin to the exact tag so the installed version always matches
    install_url = f"{GIT_BASE}@v{latest_tag}"

    uv = shutil.which("uv")
    if uv:
        pip_or_uv = [uv, "tool", "install", "--reinstall", install_url]
        label = "uv"
    else:
        pip = shutil.which("pip") or shutil.which("pip3") or "pip3"
        pip_or_uv = [pip, "install", install_url]
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

    if exit_code != 0 and exit_code != 124:
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
def config_validate() -> None:
    """Validate spyro.toml schema for correctness."""
    from ..utils.config import parse_config, parse_ssh_config

    issues: list[str] = []
    warnings: list[str] = []

    try:
        config = load_config()
    except SystemExit as e:
        console.print(f"[red]✗ Could not load config: {e}[/red]")
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

        # Duplicate forwarded ports across profiles
        all_ports: dict[int, str] = {}
        for n, p in config.profiles.items():
            for fp in p.forwarded_ports:
                if fp in all_ports and all_ports[fp] != n:
                    warnings.append(f"Port {fp} forwarded in both '{all_ports[fp]}' and '{n}'")
                all_ports[fp] = n

        # DB config
        if profile.db.name and not profile.db.host:
            warnings.append(f"[{name}] db.host is empty")

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
    ps_cmd = "ps aux --no-headers 2>/dev/null || ps aux 2>/dev/null || ps -ef 2>/dev/null"
    if grep:
        import shlex
        ps_cmd += f" | grep -i {shlex.quote(grep)}"
    ssh_args.append(ps_cmd)

    try:
        result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        console.print("[red]Process list timed out[/red]")
        return
    except FileNotFoundError:
        console.print("[red]ssh not found[/red]")
        return

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr:
            console.print(f"[red]{stderr}[/red]")
        return

    output = result.stdout.strip()
    if not output:
        console.print("[yellow]No matching processes[/yellow]")
        return

    if json_output:
        import json as json_mod
        lines = output.splitlines()
        entries = []
        for line in lines:
            parts = line.split(None, 10)
            if len(parts) >= 8:
                entries.append({
                    "user": parts[0],
                    "pid": parts[1],
                    "cpu": parts[2],
                    "mem": parts[3],
                    "vsz": parts[4],
                    "rss": parts[5],
                    "tty": parts[6],
                    "stat": parts[7],
                    "command": " ".join(parts[8:]),
                })
        console.print(json_mod.dumps(entries, indent=2))
    else:
        console.print(output)


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
    from ..core.pty_engine import _scp_target

    config = load_config()
    p = config.get_profile(profile)

    # Pull remote .env
    console.print(f"[cyan]Fetching remote .env from {p.host}...[/cyan]")
    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    remote_lines: list[str] = []

    def collect(line: str) -> None:
        remote_lines.append(line)

    runner = PTYRunner()
    remote_path = f"{p.remote_path}/.env"
    cmd = ssh_args + [f"cat {safe_quote(remote_path)}"]

    from ..utils.keychain import prompt_for_credential
    ssh_pw = prompt_for_credential(profile, p.user)
    ec = runner.run(cmd, password=ssh_pw, on_output=collect, timeout=15.0)

    if ec != 0:
        console.print("[red]Failed to pull remote .env[/red]")
        return

    remote_text = "\n".join(remote_lines)

    # Read local .env
    local_candidates = [Path.cwd() / ".env", Path(p.remote_path) / ".env"]
    local_path = None
    for cand in local_candidates:
        if cand.exists():
            local_path = cand
            break

    if not local_path:
        console.print("[yellow]No local .env found — showing remote .env only:[/yellow]")
        console.print(remote_text)
        return

    local_text = local_path.read_text(encoding="utf-8")

    # Diff
    local_lines = local_text.splitlines(keepends=True)
    remote_lines_split = remote_text.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        local_lines, remote_lines_split,
        fromfile=f"{local_path.name} (local)",
        tofile=f".env ({p.host} remote)",
        lineterm="",
    ))

    if not diff:
        console.print("[green]✓ Local and remote .env are identical[/green]")
        return

    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            console.print(f"[green]{line}[/green]")
        elif line.startswith("-") and not line.startswith("---"):
            console.print(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            console.print(f"[cyan]{line}[/cyan]")
        else:
            console.print(line)


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
        console.print(f"[red]Push failed (exit code: {ec})[/red]")

