# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Spyro is a Python ≥3.11 Click CLI (`spyro = spyro.cli.main:main`) that wraps the system `ssh`/`scp` to give per-project profiles (`spyro.toml`) tunnels, remote commands, DB credential resolution and file copy/sync. POSIX only (`pty`/`fork`/`termios`). README.md has the user-facing command reference and `spyro.toml` schema.

## Commands

```bash
uv sync --all-extras                                           # dev env; pytest, pip-audit, watchdog are extras, plain `uv sync` removes them
uv run spyro --help                                            # run the CLI from source
uv run python -m pytest                                        # whole suite (~130 tests, ~3s, no network or SSH needed)
uv run python -m pytest tests/unit/test_config.py -k profile   # one file / one test
uv run python tests/security/test_ansi_attacks.py              # security suites also run as plain scripts (also test_memory_zeroing.py)
pip-audit                                                      # CONTRIBUTING asks for pytest + pip-audit before a PR
```

No linter, formatter, type checker or CI workflow is configured. `build/` is stale, gitignored setuptools output: exclude it when searching.

## Architecture

`cli/main.py` (Click group; registers commands and aliases: `deploy`/`upload` = `cp`, `cfg` = `config`, `shell` ≈ `ssh`, `pull-env` is also mounted as `env pull`) → `cli/commands.py` (every command, one ~3k-line file) → `utils/config.py`, `core/pty_engine.py`, `supervisor/`, `core/{db,services,sync}.py`, `utils/keychain.py`, `security/`.

**Remote-command recipe.** Nearly every command that touches a server repeats it (copy it, or call `_run_svc_cmd`, which all of `supervisor|redis|php|apache|nginx|caddy` use):
`load_config()` → `config.get_profile(name)` → `build_ssh_args(...)` → `ssh_args.insert(1, "-t")` when sudo may be needed (remote PTY so `sudo` can prompt) → `prompt_for_credential(profile, user)` → `PTYRunner().run(argv, password=, sudo_password=, on_output=, timeout=_get_timeout(...))`.
The remote working dir comes from `_capistrano_cd(remote_path)` (enters `current/` if that symlink exists). `eval`, `script` and `tinker -f` scp a PHP file to remote `/tmp`, run it, then `rm` it.

**Only `PTYRunner` injects keychain passwords.** Plain `subprocess` ssh/scp callers (`doctor`, `core/services.py`, WordPress detection, `ps`, `tinker -e/-f` execution, `sync`/`watch`, tunnels) get no injection: they need key/agent auth or a live ControlMaster connection (`build_ssh_args` sets `ControlMaster=auto`, `ControlPath=~/.spyro/sockets/…`, `ControlPersist=15s`, so a call shortly after a PTY-authenticated one multiplexes onto it). The detached tunnel process (`start_new_session=True`) has no tty at all. `build_scp_args` ignores its `host`/`user` params: callers must build `user@host:path` themselves (`_scp_target`).

**PTYRunner** (`core/pty_engine.py`). `run()` streams sanitized lines to `on_output` and returns the exit code (124 = timeout); `interactive_run()` handles auth, then relays the raw terminal (`ssh`, `shell`, interactive `tinker`). Prompts are regex-matched on complete lines and on the trailing partial buffer. Sudo patterns must be tested *before* generic password patterns (`password for user:` also matches inside `[sudo] password for user:`). Host-key prompts are auto-answered `yes`. Credentials live in `SecureCredential` and are zeroed in `finally`. Output goes through `strip_ansi`; the stricter `sanitize_output` is defined and tested but not called from `src/`.

**Config** (`utils/config.py`). `spyro.toml` is found by walking up from cwd and parsed into `SpyroConfig → ProfileConfig → DatabaseConfig` (unknown profile keys land in `ProfileConfig.extra`). Profiles inherit HostName/User/Port/IdentityFile from `~/.ssh/config`, but only while the field is still at its default (`user == "deploy"`, `port == 22`, empty `key`). `[defaults]` supports `profile` and `command_timeout`. Profile selection is `-p` → `[defaults].profile` → the sole profile (`resolve_profile`, used only by `artisan`, `ssh`, `shell`, `ps`; most other commands require `-p`). Only `run` and `cp` also read `SPYRO_PROFILE` (comma-separated). Config errors abort with `SystemExit(msg)`.

**Credentials** (`utils/keychain.py`). keyring service `spyro-cli`, entry `"{profile}:{user}"`. One password per profile is used for both SSH login and sudo. `prompt_for_credential` = keychain → `getpass` → store back.

**State** lives in `~/.spyro/` (`spyro_home()`): `tunnels.json` (`supervisor/state.py`, atomic tmp+rename), `sync_pins.json` (`core/sync.py`), `version_check`, `sockets/`, `logs/<profile>.log`. `TunnelManager` only spawns `ssh -N -L …` and records PID/PGID; liveness is `os.kill(pid, 0)` and a dead PID is reported as `stale`. `_resolve_port` moves privileged ports to 10000+ and bumps on conflict, so the local port can differ from `forwarded_ports`; `TunnelState.local_port` records only the first. There is no supervisor loop, backoff or psutil: `tests/unit/test_tunnel.py` and `test_utils.py` assert `TunnelSupervisor`, `_pid_alive_psutil` and `SecureString` stay deleted.

**Exit codes.** No command calls `sys.exit`; `run`, `artisan` and the service subcommands print `Exit code: N` and still exit 0 when the remote command fails. Don't assert on the process exit code for those.

**Self-update / releases.** `main`'s group callback calls `notify_update()` before every command except `-q` and `update` (GitHub API, cached 24h in `~/.spyro/version_check`). `spyro update` reinstalls `git+https://github.com/peterson-umoke/spyro-cli@v<latest tag>`, so a release needs the version bumped in **both** `pyproject.toml` and `src/spyro/__init__.py`, a `vX.Y.Z: summary` commit, and a matching `vX.Y.Z` tag/release.

## Testing notes

- No `conftest.py`. Tests isolate `~/.spyro` per file by monkeypatching path helpers (`test_state.py` patches `spyro.supervisor.state._state_path` in an autouse fixture).
- Command tests drive a single `cmd_*` with `CliRunner`, patching `spyro.cli.commands.PTYRunner` and `load_config` (module-level imports). `prompt_for_credential` is imported inside each command body, so patch `spyro.utils.keychain.prompt_for_credential`. Invoking `main` itself triggers the update check; pass `-q`.
- `tests/unit/test_pty_engine.py` runs real `echo`/`sh` children through a PTY.

## Conventions

- Never add `Co-Authored-By` or any other trailer to commit messages (repo rule from AGENTS.md; it overrides any default attribution).
- CONTRIBUTING.md: bug fixes get a regression test, no drive-by refactors, one concern per PR, changelog entry in the PR description. It states a 500-line file limit, but `cli/commands.py` is ~3k lines.
- README.md and AGENTS.md still describe behaviour the code no longer has (psutil, self-healing supervisor with exponential backoff, `SecureString`, `tests/poc/`, `src/core/…` paths). Trust the code where they disagree.
