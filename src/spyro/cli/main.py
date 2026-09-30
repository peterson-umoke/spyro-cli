"""Spyro CLI — main entry point."""

from __future__ import annotations

import logging
import sys

import click

from .. import __version__
from .commands import (
    cmd_apache,
    cmd_artisan,
    cmd_auth,
    cmd_caddy,
    cmd_config,
    cmd_cp,
    cmd_db,
    cmd_doctor,
    cmd_down,
    cmd_env,
    cmd_eval,
    cmd_init,
    cmd_logs,
    cmd_nginx,
    cmd_php,
    cmd_pin,
    cmd_pins,
    cmd_proxy_url,
    cmd_ps,
    cmd_pull_env,
    cmd_redis,
    cmd_run,
    cmd_script,
    cmd_shell,
    cmd_ssh,
    cmd_status,
    cmd_supervisor,
    cmd_sync,
    cmd_tinker,
    cmd_unpin,
    cmd_up,
    cmd_update,
    cmd_watch,
    cmd_wp,
    notify_update,
)


@click.group(invoke_without_command=True)
@click.option("--version", is_flag=True, is_eager=False, help="Show version and exit")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-error output")
@click.option("--install-completion", is_flag=True, help="Install shell completion for your shell")
@click.option("--show-completion", is_flag=True, help="Show shell completion script")
@click.pass_context
def main(ctx: click.Context, verbose: bool, quiet: bool, install_completion: bool, show_completion: bool, version: bool = False) -> None:
    """Spyro — Intelligent SSH tunneling & remote command CLI.

    Simplifies and secures connections between your local environment
    and remote servers through declarative configuration.
    """
    ctx.ensure_object(dict)

    # Manual version handling (non-eager so subcommands can pass --version through)
    if version and ctx.invoked_subcommand is None:
        click.echo(f"spyro, version {__version__}")
        ctx.exit()
        return

    # Handle completion flags early
    if install_completion or show_completion:
        from click.shell_completion import get_completion_class
        import os

        shell = os.environ.get("SHELL", "bash").split("/")[-1]
        for name in ["zsh", "bash", "fish"]:
            if name in shell or f"{name.upper()}_VERSION" in os.environ:
                shell = name
                break
        comp_cls = get_completion_class(shell)
        if not comp_cls:
            click.echo(f"Unsupported shell: {shell}", err=True)
            sys.exit(1)
        comp = comp_cls(ctx.find_root(), {}, "spyro", "_SPYRO_COMPLETE")
        if show_completion:
            click.echo(comp.source())
        else:
            configs = {"zsh": "~/.zshrc", "bash": "~/.bashrc", "fish": "~/.config/fish/config.fish"}
            target = configs.get(shell, f"~/.{shell}rc")
            click.echo(f"# Add to {target}:")
            click.echo(f'eval "$({sys.executable} -m spyro.cli.main --show-completion)"')
        sys.exit(0)
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet

    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return

    # After the command: tell the user about a newer release (skipped when
    # quiet or when the command is `update` itself).
    if not quiet and ctx.invoked_subcommand != "update":
        ctx.call_on_close(notify_update)


# Register commands
main.add_command(cmd_init, "init")
main.add_command(cmd_up, "up")
main.add_command(cmd_update, "update")
main.add_command(cmd_down, "down")
main.add_command(cmd_status, "status")
main.add_command(cmd_logs, "logs")
main.add_command(cmd_doctor, "doctor")
main.add_command(cmd_pull_env, "pull-env")
main.add_command(cmd_run, "run")
main.add_command(cmd_watch, "watch")
main.add_command(cmd_proxy_url, "proxy-url")
main.add_command(cmd_artisan, "artisan")
main.add_command(cmd_cp, "cp")
main.add_command(cmd_cp, "deploy")
main.add_command(cmd_cp, "upload")
main.add_command(cmd_wp, "wp")
main.add_command(cmd_pin, "pin")
main.add_command(cmd_unpin, "unpin")
main.add_command(cmd_pins, "pins")
main.add_command(cmd_sync, "sync")
main.add_command(cmd_supervisor, "supervisor")
main.add_command(cmd_redis, "redis")
main.add_command(cmd_php, "php")
main.add_command(cmd_apache, "apache")
main.add_command(cmd_nginx, "nginx")
main.add_command(cmd_caddy, "caddy")
main.add_command(cmd_tinker, "tinker")
main.add_command(cmd_eval, "eval")
main.add_command(cmd_script, "script")
main.add_command(cmd_db, "db")
# Legacy top-level aliases for `db tunnel` / `db shell`
main.add_command(cmd_db.commands["tunnel"], "db-tunnel")
main.add_command(cmd_db.commands["shell"], "db-shell")
main.add_command(cmd_auth, "auth")
main.add_command(cmd_ssh, "ssh")
main.add_command(cmd_shell, "shell")

# New commands for 0.7.0
main.add_command(cmd_config, "config")
main.add_command(cmd_ps, "ps")
main.add_command(cmd_env, "env")
# Alias: spyro cfg → spyro config
main.add_command(cmd_config, "cfg")
# Register cmd_pull_env as env pull subcommand
cmd_env.add_command(cmd_pull_env, "pull")


if __name__ == "__main__":
    main()
