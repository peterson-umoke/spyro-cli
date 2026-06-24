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
    cmd_db_shell,
    cmd_db_tunnel,
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


@click.group()
@click.version_option(version=__version__, prog_name="spyro")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.option("-q", "--quiet", is_flag=True, help="Suppress non-error output")
@click.option("--install-completion", is_flag=True, help="Install shell completion for your shell")
@click.option("--show-completion", is_flag=True, help="Show shell completion script")
@click.pass_context
def main(ctx: click.Context, verbose: bool, quiet: bool, install_completion: bool, show_completion: bool) -> None:
    """Spyro — Intelligent SSH tunneling & remote command CLI.

    Simplifies and secures connections between your local environment
    and remote servers through declarative configuration.
    """
    ctx.ensure_object(dict)

    # Handle completion flags early
    if install_completion or show_completion:
        shell = click.get_current_context().parent  # not needed here
        import click.shell_completion as shcomp

        # Determine shell
        shell_name = ""
        for var in ["SHELL", "ZSH_VERSION", "BASH_VERSION"]:
            import os
            val = os.environ.get(var, "")
            if val:
                if "zsh" in val.lower() or var == "ZSH_VERSION":
                    shell_name = "zsh"
                elif "bash" in val.lower() or var == "BASH_VERSION":
                    shell_name = "bash"
                elif "fish" in val.lower():
                    shell_name = "fish"
                elif "powershell" in val.lower() or "pwsh" in val.lower():
                    shell_name = "powershell"
                break

        if not shell_name:
            shell_name = os.environ.get("SHELL", "bash").split("/")[-1] or "bash"

        # Click 8.x built-in completion
        from click.shell_completion import get_completion_class

        comp_cls = get_completion_class(shell_name)
        if comp_cls is None:
            click.echo(f"Unsupported shell: {shell_name}", err=True)
            sys.exit(1)

        comp = comp_cls(ctx.find_root(), {}, "spyro", f"_SPYRO_COMPLETE")

        if install_completion:
            # Recommend how to install
            source_cmd = {
                "bash": f"eval \"$({sys.executable} -m spyro.cli.main --show-completion)\"",
                "zsh": f"eval \"$({sys.executable} -m spyro.cli.main --show-completion)\"",
                "fish": f"{sys.executable} -m spyro.cli.main --show-completion | source",
                "powershell":
                    f'{sys.executable} -m spyro.cli.main --show-completion | Out-String | Invoke-Expression',
            }.get(shell_name, "")

            if shell_name == "zsh":
                click.echo("# Add this to your ~/.zshrc:")
            elif shell_name == "bash":
                click.echo("# Add this to your ~/.bashrc:")
            elif shell_name == "fish":
                click.echo("# Add this to your ~/.config/fish/config.fish:")
            elif shell_name == "powershell":
                click.echo("# Add this to your PowerShell profile:")

            click.echo(source_cmd)
            click.echo("\n[yellow]Then restart your shell or source the file.[/yellow]")
        else:
            # Show completion script
            script = comp.source()
            click.echo(script)

        sys.exit(0)

    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet

    # Post-command: check for updates (unless quiet or running update itself)
    if not quiet and ctx.invoked_subcommand != "update":
        notify_update()


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
main.add_command(cmd_tinker, "tinker")
main.add_command(cmd_eval, "eval")
main.add_command(cmd_script, "script")
main.add_command(cmd_db, "db")
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
