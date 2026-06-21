# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Commit Convention

- Never add Co-Authored-By or any other attribution trailers to commit messages.
- Never add commit message trailers of any kind unless the user explicitly asks for them.

## Build & Development Commands

- **Install dev dependencies**: `uv sync` (creates venv from `pyproject.toml`)
- **Run the CLI without installing**: `uv run spyro --help`
- **Install globally**: `uv tool install .` (or `uv tool install . --with watchdog`)
- **Run single test**: `python3 -m pytest tests/unit/test_config.py -v -k "test_name"` (uses `-v --tb=short` by default, configured in `pyproject.toml`)
- **Run all unit tests**: `python3 -m pytest tests/unit/ -v`
- **Run security tests**: `python3 tests/security/test_ansi_attacks.py` and `python3 tests/security/test_memory_zeroing.py`
- **Run all tests**: `python3 -m pytest tests/ -v`
- **Audit dependencies**: `pip-audit` (requires `[dev]` extras)
- **End-to-end tests**: `/tmp/test-spyro-e2e.sh` (runs all commands against a real profile)

## High-Level Architecture

Spyro is a Python CLI tool for SSH tunneling, remote command execution, and database credential resolution. The entry point is `spyro.cli.main:main`, a Click group that registers ~35 subcommands.

### Package Structure (`src/spyro/`)

- **`cli/main.py`** — Click CLI group with all subcommand registrations (up, down, artisan, db, cp, auth, doctor, eval, tinker, etc.)
- **`cli/commands.py`** — All command implementations; each is a Click command/group with Rich console output
- **`core/pty_engine.py`** — PTYRunner: spawns native `ssh` via `pty.openpty()` + `os.fork()`, matches auth/sudo prompt regexes, injects credentials into the PTY buffer, hands off to raw terminal relay. Also builds `ssh`/`scp` argument lists.
- **`core/db.py`** — Database credential resolution (local config vs remote `.env` scan) and connection URL generation for MySQL/PostgreSQL/SQLite
- **`core/services.py`** — Remote service detection (Redis, Supervisor, PHP-FPM, Node.js, Apache, Nginx, Caddy) used by `spyro doctor`
- **`core/sync.py`** — SyncPin dataclass, framework-aware exclusion rules (Laravel, WordPress, Node, Python), and `should_exclude()` logic for `spyro sync`/`watch`
- **`supervisor/tunnel.py`** — TunnelManager: self-healing SSH tunnel daemon management with `_port_available()`, `_resolve_port()`, psutil-based process tree kill, exponential backoff restart
- **`supervisor/state.py`** — TunnelState dataclass, JSON persistence via `~/.spyro/tunnels.json`
- **`security/memory.py`** — `SecureCredential` and `SecureString`: mutable `bytearray` wrappers with triple-pass zeroing (zero → random → zero), context manager support, destructor-based cleanup
- **`security/ansi.py`** — `sanitize_output()`: strips CSI, OSC, DCS, C0, charset, and fallback escape sequences; defends against terminal injection attacks from remote output
- **`utils/config.py`** — `SpyroConfig`/`ProfileConfig`/`DatabaseConfig` dataclasses, `spyro.toml` parsing via `tomllib`, SSH config (`~/.ssh/config`) inheritance
- **`utils/keychain.py`** — OS keychain wrapper via `keyring` library (`spyro-cli` service), with `prompt_for_credential()` fallback chain: keychain → getpass prompt → store
- **`utils/paths.py`** — `discover_config()` (walks up from cwd for `spyro.toml`), `spyro_home()` (`~/.spyro/`), `safe_quote()` for shell argument escaping
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
- Remote output passes through `sanitize_output()` before printing (strips ANSI terminal escape sequences)
- Shell arguments quoted with `shlex.quote()` via `safe_quote()`
- Passwords stored only in OS keychain (macOS Keychain / Linux Secret Service), never in config files

### Project Configuration

- Build: setuptools (`pyproject.toml`), packages found in `src/`
- Python: >= 3.11 (uses stdlib `tomllib`)
- Test config: pytest with `testpaths = ["tests"]`, default `-v --tb=short`
- Dependencies: click, rich, psutil, keyring
- Optional: watchdog (for `spyro sync/watch`), pytest/pytest-cov/pip-audit (dev)
