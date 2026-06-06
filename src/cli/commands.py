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
