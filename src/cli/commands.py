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

    # Remote service detection
    console.print("\n[bold]6. Remote services[/bold]")
    for name, profile in config.profiles.items():
        console.print(f"\n  [cyan]{name}[/cyan] ({profile.host})")
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

    artisan_cmd = f"cd {safe_quote(p.remote_path)} && php artisan {' '.join(safe_quote(a) for a in cmd_args)}"

    if not no_escalate and p.sudo:
        artisan_cmd = f"sudo {artisan_cmd}"

    ssh_args = build_ssh_args(
        host=p.host,
        user=p.user,
        port=p.port,
        key=p.key,
    )
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
    wp_cmd = f"cd {safe_quote(p.remote_path)} && {wp_bin} {' '.join(safe_quote(a) for a in cmd_args)}"

    if not no_escalate and p.sudo:
        wp_cmd = f"sudo {wp_cmd}"

    ssh_args.append(wp_cmd)

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

    if src.startswith("/") or src.startswith("~"):
        remote_src = f"{p.host}:{src}"
        scp_args = build_scp_args(
            src=remote_src,
            dest=dest,
            host=p.host,
            user=p.user,
            port=p.port,
            key=p.key,
            recursive=recursive,
        )
    else:
        remote_dest = f"{p.host}:{dest}"
        scp_args = build_scp_args(
            src=src,
            dest=remote_dest,
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
