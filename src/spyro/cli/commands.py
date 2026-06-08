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
def cmd_status(profile: str | None) -> None:
    """Display health, active tunnels, and port mappings."""
    config = load_config()
    manager = TunnelManager(config)
    statuses = manager.status(profile)

    if not statuses:
        console.print("[yellow]No tunnels configured[/yellow]")
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


# ---------------------------------------------------------------------------
# spyro logs
# ---------------------------------------------------------------------------


@click.command()
@click.argument("profile", required=False)
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
def cmd_logs(profile: str | None, follow: bool) -> None:
    """Stream supervisor logs for a profile."""
    from ..utils.paths import spyro_home

    log_dir = spyro_home() / "logs"

    if profile:
        log_file = log_dir / f"{profile}.log"
        if not log_file.exists():
            console.print(f"[red]No log file for '{profile}'[/red]")
            return
        _show_log(log_file, follow)
    else:
        if not log_dir.exists():
            console.print("[yellow]No log files found[/yellow]")
            return

        for log_file in sorted(log_dir.glob("*.log")):
            console.print(f"\n[bold cyan]--- {log_file.stem} ---[/bold cyan]")
            _show_log(log_file, follow)


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
def cmd_doctor() -> None:
    """Run automated diagnostics."""
    console.print("[bold cyan]Spyro Doctor[/bold cyan]\n")

    issues = []

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
                console.print(f"  [green]✓[/green] {name}: reachable")
            else:
                console.print(f"  [red]✗[/red] {name}: connection failed")
                issues.append(f"SSH to {name} failed")
        except subprocess.TimeoutExpired:
            console.print(f"  [yellow]⚠[/yellow] {name}: timeout")
            issues.append(f"SSH to {name} timed out")
        except FileNotFoundError:
            console.print(f"  [red]✗[/red] ssh not found")
            issues.append("ssh binary not found")
            break

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
                console.print(f"  [green]✓[/green] {name}: {profile.remote_path} exists")
            else:
                console.print(f"  [yellow]⚠[/yellow] {name}: {profile.remote_path} not found")
                issues.append(f"Remote path missing on {name}")
        except Exception:
            console.print(f"  [yellow]⚠[/yellow] {name}: could not verify")

    console.print("\n[bold]3. Local port conflicts[/bold]")
    from ..supervisor.tunnel import _port_available

    for name, profile in config.profiles.items():
        for port in profile.forwarded_ports:
            if _port_available(port):
                console.print(f"  [green]✓[/green] Port {port}: available")
            else:
                console.print(f"  [yellow]⚠[/yellow] Port {port}: in use")
                issues.append(f"Port {port} conflict for {name}")

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
        artisan_path = f"{profile.remote_path}/artisan"
        ssh_args.extend(["test", "-f", artisan_path])

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                console.print(f"  [green]✓[/green] {name}: artisan found")
            else:
                console.print(f"  [red]✗[/red] {name}: artisan not found")
                issues.append(f"Artisan not found on {name}")
        except Exception:
            console.print(f"  [yellow]⚠[/yellow] {name}: could not verify")

    # WordPress detection
    wp_profiles = [n for n, p in config.profiles.items() if p.wordpress]
    if wp_profiles:
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

            if indicators["wp_config"]:
                console.print(f"  [green]✓[/green] {name}: WordPress detected")
                if indicators["wp_cli"]:
                    console.print(f"    [green]✓[/green] WP-CLI available")
                else:
                    console.print(f"    [yellow]⚠[/yellow] WP-CLI not found")
                    issues.append(f"WP-CLI not found on {name}")
            else:
                console.print(f"  [yellow]⚠[/yellow] {name}: WordPress not detected")
                issues.append(f"WordPress not detected on {name} (wordpress=true in config)")

    # Remote service detection (with per-profile timeout guard)
    console.print("\n[bold]6. Remote services[/bold]")
    for name, profile in config.profiles.items():
        console.print(f"\n  [cyan]{name}[/cyan] ({profile.host})")
        try:
            services = detect_all_services(
                host=profile.host,
                user=profile.user,
                port=profile.port,
                key=profile.key,
            )
            for svc in services:
                line = f"    {svc.icon} {svc.summary}"
                if svc.path:
                    line += f" ({svc.path})"
                console.print(line)
                if svc.details:
                    for k, v in svc.details.items():
                        console.print(f"      {k}: {v}")
        except Exception as e:
            console.print(f"    [yellow]⚠ Service check interrupted: {e}[/yellow]")
            issues.append(f"Service check failed for {name}: {e}")

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
            sudo_pw = prompt_for_credential(name, "sudo", p.user)

        ssh_pw = prompt_for_credential(name, "ssh", p.user)

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
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_artisan(cmd_args: tuple[str, ...], no_escalate: bool, profile: str) -> None:
    """Run Laravel Artisan commands on the remote host."""
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

    artisan_cmd = f"cd {safe_quote(p.remote_path)} && {sudo_prefix}php artisan {' '.join(safe_quote(a) for a in cmd_args)}"

    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )

    # Force PTY allocation so remote sudo can prompt for password
    if p.sudo and not no_escalate:
        ssh_args.insert(1, "-t")

    ssh_args.append(artisan_cmd)

    from ..utils.keychain import prompt_for_credential

    sudo_pw = ""
    if p.sudo and not no_escalate:
        sudo_pw = prompt_for_credential(profile, "sudo", p.user)

    ssh_pw = prompt_for_credential(profile, "ssh", p.user)

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
def cmd_pins() -> None:
    """List all pinned sync directories."""
    pins = load_pins()
    if not pins:
        console.print("[yellow]No pinned directories. Use 'spyro pin' to add one.[/yellow]")
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

    # Build command
    sudo_prefix = ""
    if not no_escalate and p.sudo:
        sudo_prefix = "sudo "

    wp_cmd = f"cd {safe_quote(p.remote_path)} && {sudo_prefix}{wp_bin} {' '.join(safe_quote(a) for a in cmd_args)}"

    ssh_args.append(wp_cmd)

    # Force PTY allocation so remote sudo can prompt for password
    if p.sudo and not no_escalate:
        ssh_args.insert(1, "-t")

    from ..utils.keychain import prompt_for_credential

    sudo_pw = ""
    if p.sudo and not no_escalate:
        sudo_pw = prompt_for_credential(profile, "sudo", p.user)

    ssh_pw = prompt_for_credential(profile, "ssh", p.user)

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
    """Check if a path is a local absolute path (not a remote scp-style path).

    A path starting with ':' is always treated as a remote path.
    """
    if path.startswith(":"):
        return False
    return path.startswith("/") or path.startswith("~") or path.startswith("./")


@click.command()
@click.argument("src")
@click.argument("dest")
@click.option("--recursive", "-r", is_flag=True, help="Copy directories")
@click.option("--profile", "-p", required=True, help="Profile name")
def cmd_cp(src: str, dest: str, recursive: bool, profile: str) -> None:
    """Securely copy files with auto-sudo escalation."""
    config = load_config()
    p = config.get_profile(profile)

    runner = PTYRunner()

    src_is_local = _is_local_path(src)

    from ..core.pty_engine import _scp_target

    if src_is_local:
        # Local -> remote (dest is on the remote host via profile)
        scp_args = build_scp_args(
            src=src,
            dest=_scp_target(dest, p.host, p.user),
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
            recursive=recursive,
        )
    else:
        # Remote -> local (dest is a local path)
        scp_args = build_scp_args(
            src=_scp_target(src, p.host, p.user),
            dest=dest,
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
            recursive=recursive,
        )

    console.print(f"[cyan]Copying {src} -> {dest}...[/cyan]")

    from ..utils.keychain import prompt_for_credential

    ssh_pw = prompt_for_credential(profile, "ssh", p.user)

    def output_line(line: str) -> None:
        console.print(line)

    exit_code = runner.run(
        scp_args,
        password=ssh_pw,
        on_output=output_line,
        timeout=120.0,
    )

    if exit_code == 0:
        console.print("[green]Copy complete[/green]")
    else:
        console.print(f"[red]Copy failed (exit code: {exit_code})[/red]")


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
@click.option("--type", "cred_type", type=click.Choice(["ssh", "sudo"]), default=None,
              help="Credential type (omit for both SSH + sudo)")
@click.option("--password", "-w", default="", help="Password (omit to prompt)")
def set(profile: str, cred_type: str | None, password: str) -> None:
    """Store a credential in the OS keychain.

    If --password is omitted, you'll be prompted securely (no echo).
    If --type is omitted, both SSH and sudo passwords are stored.
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

    types = [cred_type] if cred_type else ["ssh", "sudo"]

    for t in types:
        # Check existing
        existing = get_credential(profile, t, username)
        if existing:
            console.print(f"[yellow]  {t} credential for {username}@{profile} already exists[/yellow]")
            if not click.confirm(f"  Overwrite?"):
                continue

        pw = password or getpass.getpass(f"  {t} password for {username}@{profile}: ")
        if not pw:
            console.print(f"  [red]No password provided, skipping {t}[/red]")
            continue

        if store_credential(profile, t, username, pw):
            console.print(f"[green]  ✓ {t} credential stored for {username}@{profile}[/green]")
        else:
            console.print(f"[red]  ✗ Failed to store {t} credential[/red]")


@cmd_auth.command()
@click.option("--profile", "-p", required=True, help="Profile name")
@click.option("--type", "cred_type", type=click.Choice(["ssh", "sudo"]), default=None,
              help="Credential type (omit to delete both)")
def delete(profile: str, cred_type: str | None) -> None:
    """Remove stored credentials from the OS keychain."""
    from ..utils.keychain import delete_credential, get_credential

    config = load_config()
    try:
        p = config.get_profile(profile)
        username = p.user
    except Exception:
        username = profile

    types = [cred_type] if cred_type else ["ssh", "sudo"]

    for t in types:
        if get_credential(profile, t, username):
            if delete_credential(profile, t, username):
                console.print(f"[green]  ✓ {t} credential deleted for {username}@{profile}[/green]")
            else:
                console.print(f"[red]  ✗ Failed to delete {t} credential[/red]")
        else:
            console.print(f"  [yellow]No {t} credential found for {username}@{profile}[/yellow]")


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
            for t in ["ssh", "sudo"]:
                pw = get_credential(name, t, username)
                if pw is not None:
                    masked = pw[:2] + "••••" + pw[-2:] if len(pw) > 4 else "••••"
                    console.print(f"  [green]✓[/green] {name} {t}: {username} / {masked}")
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

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)
    if p.sudo:
        ssh_args.insert(1, "-t")
    ssh_args.append(cmd)

    from ..utils.keychain import prompt_for_credential

    sudo_pw = prompt_for_credential(profile, "sudo", p.user) if p.sudo else ""
    ssh_pw = prompt_for_credential(profile, "ssh", p.user)

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

    if eval:
        tinker_cmd = f"cd {safe_quote(p.remote_path)} && {sudo_prefix}php artisan tinker --execute={safe_quote(eval)}"
    elif file:
        remote_path = f"/tmp/spyro-tinker-{os.path.basename(file)}"
        console.print(f"[cyan]Uploading {file} to {p.host}:{remote_path}...[/cyan]")
        from ..core.pty_engine import _scp_target
        scp_args = build_scp_args(
            src=file,
            dest=_scp_target(remote_path, p.host, p.user),
            host=p.host, user=p.user, port=p.port, key=p.key,
        )
        from ..utils.keychain import prompt_for_credential as pfc
        ssh_pw = pfc(profile, "ssh", p.user) if not no_escalate else ""
        runner.run(scp_args, password=ssh_pw, timeout=30)
        tinker_cmd = f"cd {safe_quote(p.remote_path)} && {sudo_prefix}php artisan tinker < {remote_path}; {sudo_prefix}rm -f {remote_path}"
    else:
        tinker_cmd = f"cd {safe_quote(p.remote_path)} && {sudo_prefix}php artisan tinker"

    ssh_args = build_ssh_args(host=p.host, user=p.user, port=p.port, key=p.key)

    if not no_escalate and p.sudo:
        ssh_args.insert(1, "-t")

    if not eval and not file:
        ssh_args.insert(1, "-tt")

    ssh_args.append(tinker_cmd)

    from ..utils.keychain import prompt_for_credential as pfc2

    sudo_pw = pfc2(profile, "sudo", p.user) if p.sudo and not no_escalate else ""
    ssh_pw = pfc2(profile, "ssh", p.user)

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
        exit_code = runner.run(
            ssh_args, password=ssh_pw, sudo_password=sudo_pw,
            on_output=lambda line: None, timeout=30,
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

    ssh_pw = prompt_for_credential(profile, "ssh", p.user)
    sudo_pw = prompt_for_credential(profile, "sudo", p.user) if p.sudo else ""

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
