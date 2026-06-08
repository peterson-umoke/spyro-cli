"""Spyro CLI — main entry point."""

from __future__ import annotations

import logging

import click

from .. import __version__
from .commands import (
    cmd_cp,
    cmd_doctor,
    cmd_down,
    cmd_init,
    cmd_logs,
    cmd_proxy_url,
    cmd_pull_env,
    cmd_run,
    cmd_status,
    cmd_up,
    cmd_watch,
    cmd_artisan,
    cmd_wp,
    cmd_pin,
    cmd_unpin,
    cmd_pins,
    cmd_sync,
    cmd_db_shell,
    cmd_db_tunnel,
    cmd_supervisor,
    cmd_redis,
    cmd_php,
    cmd_apache,
    cmd_nginx,
    cmd_caddy,
)


@click.group()
@click.version_option(version=__version__, prog_name="spyro")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-error output")
@click.pass_context
def main(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """Spyro — Intelligent SSH tunneling & remote command CLI.

    Simplifies and secures connections between your local environment
    and remote servers through declarative configuration.
    """
    ctx.ensure_object(dict)

    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet


# Register commands
main.add_command(cmd_init, "init")
main.add_command(cmd_up, "up")
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
main.add_command(cmd_db_tunnel, "db-tunnel")
main.add_command(cmd_db_shell, "db-shell")
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


if __name__ == "__main__":
    main()
