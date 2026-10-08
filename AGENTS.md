# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Commit Convention

- Never add Co-Authored-By or any other attribution trailers to commit messages.
- Never add commit message trailers of any kind unless the user explicitly asks for them.

## Versioning (binding for every agent and human)

Format `MAJOR.MINOR.PATCH`, defined by [Semantic Versioning 2.0.0](https://semver.org) (the industry standard):

| Change | Bump | Example |
|---|---|---|
| Bug fix, docs, internal refactor, no user-visible behaviour change | PATCH | `1.2.3` → `1.2.4` |
| Backwards-compatible new command, flag or feature | MINOR, PATCH resets to 0 | `1.2.4` → `1.3.0` |
| Breaking change: removed/renamed command or flag, changed exit codes or `--json` shape, `spyro.toml` incompatibility | MAJOR, MINOR and PATCH reset to 0 | `1.3.0` → `2.0.0` |

**Spyro-specific rule (NOT part of SemVer): the minor number never exceeds 99.** Once the version is `MAJOR.99.PATCH`, the next release is `(MAJOR+1).0.0`, never `MAJOR.100.0` (e.g. `1.99.0` → `2.0.0`, `1.99.4` → `2.0.0`). Strict SemVer raises MAJOR only for breaking changes, so a rollover release may be non-breaking; say so in its notes. Consequence: no patch releases inside `X.99`, so a hotfix there ships as the next major; finish hotfixes while the minor is still below 99. History: `0.9.1` → `1.0.0` was made under an earlier cap of 9; the cap is now 99.

Decide the bump from what a user's scripts or config would notice, not from diff size. Files to bump and release steps: "Self-update / releases" in `CLAUDE.md`. "Release" means the whole chain, done without asking: bump the version files, `uv lock`, commit, tag, push `main` and the tag, then `gh release create vX.Y.Z --latest` with notes. A tag alone is not a release.

## Build & Development Commands

- **Install dev dependencies**: `uv sync --all-extras` (pytest, pip-audit and watchdog are extras; plain `uv sync` removes them)
- **Run the CLI without installing**: `uv run spyro --help`
- **Install globally**: `uv tool install .` (or `uv tool install . --with watchdog`)
- **Run single test**: `uv run python -m pytest tests/unit/test_config.py -v -k "test_name"` (uses `-v --tb=short` by default, configured in `pyproject.toml`)
- **Run all unit tests**: `uv run python -m pytest tests/unit/ -v`
- **Run security tests**: `python3 tests/security/test_ansi_attacks.py` and `python3 tests/security/test_memory_zeroing.py`
- **Run all tests**: `uv run python -m pytest tests/ -v`
- **Audit dependencies**: `pip-audit` (requires `[dev]` extras)
- **End-to-end tests**: `/tmp/test-spyro-e2e.sh` (runs all commands against a real profile)

## High-Level Architecture

Spyro is a Python CLI tool for SSH tunneling, remote command execution, and database credential resolution. The entry point is `spyro.cli.main:main`, a Click group that registers ~35 subcommands.

### Package Structure (`src/spyro/`)

- **`cli/main.py`** — Click CLI group with all subcommand registrations (up, down, artisan, db, cp, auth, doctor, eval, tinker, etc.)
- **`cli/commands.py`** — All command implementations; each is a Click command/group with Rich console output
- **`core/pty_engine.py`** — PTYRunner: spawns native `ssh` via `pty.openpty()` + `os.fork()`, matches auth/sudo prompt regexes, injects credentials into the PTY buffer, hands off to raw terminal relay. Also builds `ssh`/`scp` argument lists.
- **`core/db.py`** — Database credential resolution (local config vs remote `.env` scan), connection URL generation, and local client argv/env (passwords go via `MYSQL_PWD`/`PGPASSWORD`)
- **`core/services.py`** — Remote service detection (Redis, Supervisor, PHP-FPM, Node.js, Apache, Nginx, Caddy) used by `spyro doctor`
- **`core/sync.py`** — SyncPin dataclass, framework-aware exclusion rules (Laravel, WordPress, Node, Python), and `should_exclude()` logic for `spyro sync`
- **`supervisor/tunnel.py`** — TunnelManager: `ssh -f -N -L` through the PTY engine (ControlMaster per tunnel), verified before success is reported; `stop()` uses the control socket and only signals a PID that is still ssh/spyro. No restart loop: a dead tunnel shows as `stale`
- **`supervisor/state.py`** — TunnelState dataclass, JSON persistence via `~/.spyro/tunnels.json` (flock + atomic write), `tunnel_alive()` PID identity check
- **`security/memory.py`** — `SecureCredential`: mutable `bytearray` wrapper with triple-pass zeroing (zero → random → zero), context manager support, destructor-based cleanup
- **`security/ansi.py`** — `strip_ansi()` (`sanitize_output` is an alias): strips every escape sequence, C1 controls, NUL, BEL and resolves CR; defends against terminal injection from remote output
- **`utils/config.py`** — `SpyroConfig`/`ProfileConfig`/`DatabaseConfig` dataclasses, `spyro.toml` parsing via `tomllib`, SSH config (`~/.ssh/config`) inheritance
- **`utils/keychain.py`** — OS keychain wrapper via `keyring` library (`spyro-cli` service), with `prompt_for_credential()` fallback chain: `SPYRO_PASSWORD[_<PROFILE>]` env → keychain → getpass prompt (only on a tty) → store
- **`utils/paths.py`** — `discover_config()` (walks up from cwd for `spyro.toml`), `spyro_home()` (`~/.spyro/`), `safe_quote()` for shell argument escaping
- **`skills.py`** — Agent Skills generator: `generate()` (one `SKILL.md` per command from the Click tree + `NOTES`), `detect_targets()`, `install()`; backs `spyro install ai-skills`. The checked-in `skills/` dir must match `generate()` — regenerate with `uv run spyro install ai-skills --dest skills`
- **`tests/unit/test_eval.py`** — Unit tests for `build_eval_php()`, the PHP code generator behind `spyro eval`

### Key Data Flow

```
spyro.toml → Config (tomllib) → ProfileConfig(dataclass)
  ↓
CLI (Click) routes to command handler
  ↓
commands.py calls:
  - PTYRunner.run() for remote commands (spawns native ssh in PTY)
  - TunnelManager.start() for port forwarding daemons
  - resolve_db_credentials() for DB connection strings
  ↓
Credentials flow:
  keychain.py → SecureCredential (bytearray) → PTY buffer → zeroed
```

### Security Model

- All SSH credentials flow through `SecureCredential` (zeroed after use, not exposed in process env)
- Remote output passes through `strip_ansi()` before printing and is printed with `_remote()` (never parsed as Rich markup)
- Shell arguments quoted with `shlex.quote()` via `safe_quote()`
- Passwords stored only in OS keychain (macOS Keychain / Linux Secret Service), never in config files

### Project Configuration

- Build: setuptools (`pyproject.toml`), packages found in `src/`
- Python: >= 3.11 (uses stdlib `tomllib`)
- Test config: pytest with `testpaths = ["tests"]`, default `-v --tb=short`
- Dependencies: click, rich, keyring
- Optional: watchdog (for `spyro sync`), pytest/pytest-cov/pip-audit (dev)
