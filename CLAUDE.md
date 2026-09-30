# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Spyro is a Python ≥3.11 Click CLI (`spyro = spyro.cli.main:main`) that wraps the system `ssh`/`scp` to give per-project profiles (`spyro.toml`) tunnels, remote commands, DB credential resolution and file copy/sync. POSIX only (`pty`/`fork`/`termios`). README.md has the user-facing command reference and `spyro.toml` schema.

## Commands

```bash
uv sync --all-extras                                           # dev env; pytest, pip-audit, watchdog are extras, plain `uv sync` removes them
uv run spyro --help                                            # run the CLI from source
uv run python -m pytest                                        # whole suite (~245 tests, ~15s, no network or SSH needed)
uv run python -m pytest tests/unit/test_config.py -k profile   # one file / one test
uv run python tests/security/test_ansi_attacks.py              # security suites also run as plain scripts (also test_memory_zeroing.py)
```

CI (`.github/workflows/tests.yml`) runs the suite on Linux + macOS, Python 3.11 and 3.13, plus `pip-audit` on the runtime dependencies only (`pip-audit` on the dev env reports pip/urllib3 from the tooling). No linter, formatter or type checker is configured. `build/` is stale, gitignored setuptools output: exclude it when searching.

## Architecture

`cli/main.py` (Click group; registers commands and aliases: `deploy`/`upload` = `cp`, `cfg` = `config`, `shell` ≈ `ssh`, `db-tunnel`/`db-shell` = `db tunnel`/`db shell`, `pull-env` is also mounted as `env pull`) → `cli/commands.py` (every command, one ~3k-line file) → `utils/config.py`, `core/pty_engine.py`, `supervisor/`, `core/{db,services,sync}.py`, `utils/keychain.py`, `security/`.

**Remote-command recipe.** Commands that run something on a server go through `_run_svc_cmd(profile, cmd, timeout, cli_timeout, show_exit_code, escalate, prefix)`: `build_ssh_args` → `-t` when `escalate` (remote tty so `sudo` can prompt) → `prompt_for_credential` → `PTYRunner().run(...)` → `_report_exit` (explains a 124 timeout). `run`, `artisan`, `wp`, `tinker -e`, the service groups and `_upload_and_run` all use it. `eval`, `script` and `tinker -f` go through `_upload_and_run` (scp a PHP file to `/tmp/spyro-<label>-<128-bit random>.php`, run it, always `rm` it). App commands build their prefix with `_sudo_prefix(p, no_escalate)` (`sudo -u <sudo_user>` when configured; bare `sudo` runs the app as root and leaves root-owned files in `storage/`). The remote working dir comes from `_capistrano_cd(remote_path)`.

**Output rules.** Print remote text only with `_remote(line, prefix)` (a `rich.Text`: no markup, emoji or wrapping — `[/var/www]` in a log line used to crash `console.print`). Print JSON only with `_emit_json` (plain `click.echo`; Rich wraps long lines and breaks JSON). The update notice goes to stderr after the command.

**Only `PTYRunner` injects keychain passwords.** Plain `subprocess` ssh callers (`doctor`, `core/services.py`, WordPress detection, `ps`) get no injection: they need key/agent auth or a live ControlMaster connection (`build_ssh_args` sets `ControlMaster=auto`, `ControlPath=~/.spyro/sockets/%C` — `%C` is a fixed-length hash, keeping under the 104-char socket limit — and `ControlPersist=15s`). Tunnels, `sync`/`watch` and `db dump` authenticate through `PTYRunner` first; `dump` then streams over a plain ssh that reuses that master (`BatchMode=yes` is inserted *before* the existing `BatchMode=no` because ssh keeps the first `-o` value). `build_scp_args` ignores its `host`/`user` params: callers must build `user@host:path` themselves (`_scp_target`). Host keys: `accept-new` (trust on first use, refuse changes).

**PTYRunner** (`core/pty_engine.py`). `run()` streams sanitized lines to `on_output` and returns the exit code: 124 timeout (`timeout=None` waits forever), 255 when SSH asks for a password that is missing or was rejected (fails immediately, message goes to `on_output`), 127 when the program cannot run. `interactive_run()` handles auth, then relays the raw terminal and tracks `SIGWINCH`; its `timeout` bounds the auth phase only. Password prompts are anchored to the start of the line (`user@host's password:`, `Password:`) so a remote program's "Enter password:" never receives the SSH password. Sudo patterns are tested *before* generic ones. `_spawn` forks; the child must `os._exit` if exec fails. After PTY EOF the exit status comes from `_exit_status` (waiting for the child) — returning 0 there once reported failed commands as success. Output goes through `strip_ansi` (`sanitize_output` is an alias): it drops every escape sequence, C1 controls and `\r`, so captured text has clean `\n` line endings.

**Config** (`utils/config.py`). `spyro.toml` is found by walking up from cwd and parsed into `SpyroConfig → ProfileConfig → DatabaseConfig` (unknown profile keys land in `ProfileConfig.extra`). Bad files exit with a one-line `SystemExit` message, never a traceback. Profiles inherit HostName/User/Port/IdentityFile from `~/.ssh/config` (Host block for the profile name, then its `host`, layered over `Host *`, which never supplies HostName); User/Port/IdentityFile only apply to keys the profile did not set in its TOML table. `[defaults]` supports `profile` and `command_timeout`. Profile selection is `-p` → `[defaults].profile` → the sole profile (`resolve_profile`, used only by `artisan`, `ssh`, `shell`, `ps`; most other commands require `-p`). Only `run` and `cp` also read `SPYRO_PROFILE` (comma-separated). `forwarded_ports` are *remote* ports; `db.port` must be one of them.

**DB.** Commands get `(profile, DatabaseConfig, local_port)` from `_db_target`: an empty `db.password` triggers `resolve_db_credentials` (remote `.env`); the local port comes from `db_local_port` (the forward of `db.port`, not "the first forward"); `_tunnel_for` starts a tunnel if none is alive. Passwords reach local clients via `MYSQL_PWD`/`PGPASSWORD`, and reach remote `mysqldump` over stdin — never on a command line.

**Credentials** (`utils/keychain.py`). Order: `SPYRO_PASSWORD_<PROFILE>` / `SPYRO_PASSWORD` env (headless/CI) → keyring service `spyro-cli`, entry `"{profile}:{user}"` → `getpass` (only on a tty; otherwise `""`). One password per profile is used for both SSH login and sudo.

**State** lives in `~/.spyro/` (`spyro_home()`): `tunnels.json` (`supervisor/state.py`: flock + atomic replace; a corrupt file is moved to `.corrupt`), `sync_pins.json` (`core/sync.py`, same care), `version_check`, `sockets/`, `logs/<profile>.log`. A daemon tunnel is `ssh -f -N -L` run through `PTYRunner` as its own ControlMaster (`sockets/tun-<hash>`): `ssh -f` only backgrounds after auth and forwards succeed, `TunnelManager` then reads the pid via `ssh -O check` and waits for the local ports. `stop` uses `ssh -O exit`; signals are a fallback and only go to a PID that `tunnel_alive` confirms is still ssh/spyro (PIDs are recycled across reboots). `_resolve_port` moves privileged ports to 10000+ and bumps on conflict, so the local port can differ from `forwarded_ports`; `TunnelState.forwarded_ports` holds local ports in profile order. There is no restart loop, backoff or psutil: a dead tunnel reports `stale`, and `ensure()` starts a new one. Tests assert `TunnelSupervisor`, `_pid_alive_psutil` and `SecureString` stay deleted.

**Sync** (`core/sync.py`, `_run_sync_watch`). Dir exclusion patterns may be single segments or paths (`storage/framework/sessions/`); unknown frameworks get the secrets/cache rules but not `.htaccess`/`build/`/`dist/` (`_ONLY_WHEN_DETECTED`). The watchdog handler only queues created/modified/moved events (not "opened": scp reading a file would retrigger itself); the main thread uploads.

**Exit codes.** `run`, `artisan` and the service subcommands print `Exit code: N` and still exit 0 when the remote command fails — don't assert on the process exit code for those. `up`, `cp`, `db …`, `pull-env` and `env push` exit non-zero on failure (`click.ClickException` / `SystemExit(1)`).

**Self-update / releases.** `main` registers `notify_update` with `ctx.call_on_close` (skipped for `-q`/`update`): stderr only, tty only, cached 24h (failures 1h). `spyro update` reinstalls `git+https://github.com/peterson-umoke/spyro-cli@v<latest tag>`, so a release needs the version bumped in **both** `pyproject.toml` and `src/spyro/__init__.py` (and `uv lock` for `uv.lock`), a `vX.Y.Z: summary` commit, and a matching `vX.Y.Z` tag pushed to GitHub (a GitHub release is optional; the updater falls back to tags).

## Testing notes

- No `conftest.py`. Tests isolate `~/.spyro` per file by monkeypatching path helpers (`test_state.py` patches `spyro.supervisor.state._state_path`) or by setting `HOME` (`test_tunnel.py`, `test_cli_fixes.py`).
- Command tests drive the CLI with `CliRunner` (`invoke("artisan", ...)` helper in `test_cli_fixes.py` adds `-q`), patching `spyro.cli.commands.PTYRunner` and `load_config` (module-level imports). `prompt_for_credential` is imported inside command bodies, so patch `spyro.utils.keychain.prompt_for_credential` — or just set `SPYRO_PASSWORD`.
- For end-to-end behaviour use a fake `ssh` script on `PATH` (`FAKE_SSH` in `test_tunnel.py` prompts for a password and, with `-f`, forks a real listener; `FAKE_DUMP_SSH` in `test_cli_fixes.py` streams bytes). Stub out `_detect_db_client` when a real `mariadb`/`mysql` may be installed.
- Never define a module-level name that shadows a builtin in `commands.py` (`def set(...)` for `auth set` once broke every later `set()` call; it is `set_credential` now).
- The `.venv/bin/pytest` and `pip-audit` launchers on the maintainer's machine carry a stale shebang (repo moved); use `python -m pytest` / `python -m pip_audit`.

## Conventions

- Never add `Co-Authored-By` or any other trailer to commit messages (repo rule from AGENTS.md; it overrides any default attribution).
- CONTRIBUTING.md: bug fixes get a regression test, no drive-by refactors, one concern per PR, changelog entry in the PR description. It states a 500-line file limit, but `cli/commands.py` is ~3k lines.
