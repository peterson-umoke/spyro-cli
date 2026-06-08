# Spyro

Intelligent SSH tunneling and remote command CLI for developers.

Spyro automates SSH port-forwarding, remote command execution, database credential resolution, and file synchronization through a declarative `spyro.toml` configuration. It replaces manual `ssh -L` coordination, `scp` routines, and `autossh` daemons with a single, self-healing tool.

## Why Spyro

Developers working with remote servers spend significant time on repetitive SSH boilerplate: setting up port forwards, copying `.env` files, running artisan commands, syncing code. Spyro eliminates this by:

- **Automating tunnels** with a self-healing supervisor that survives network drops, sleep/wake cycles, and process crashes
- **Resolving credentials** from either local TOML config or remote `.env`/Rails config files
- **Running remote commands** through a PTY engine that handles sudo escalation without leaking passwords
- **Syncing files** in real-time using native OS filesystem watchers

## Installation

```bash
# Install spyro and core dependencies
cd spyro
pip install -e .

# Optional: install filesystem watcher for spyro sync/watch
pip install spyro-cli[watch]

# For development
pip install -e ".[dev]"
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `click` | CLI framework |
| `rich` | Terminal output formatting |
| `psutil` | Cross-platform process tree management |
| `keyring` | Native OS keychain integration (macOS Keychain, Linux Secret Service) |
| `watchdog` | Filesystem watching for `spyro watch` (optional — install via `spyro-cli[watch]`) |

### Credential Storage

Spyro stores SSH and sudo passwords in your OS keychain (macOS Keychain, Linux Secret Service).
Store them before first use:

```python
import keyring
keyring.set_password("spyro-cli", "profile:ssh:username", "your-ssh-password")
keyring.set_password("spyro-cli", "profile:sudo:username", "your-sudo-password")
```

If no keychain entry exists, Spyro falls back to prompting via `getpass`.

## Quick Start

```bash
# 1. Create configuration
spyro init

# 2. Edit spyro.toml with your server details
vim spyro.toml

# 3. Start tunnels
spyro up staging

# 4. Check status
spyro status

# 5. Run a remote command
spyro run --profile staging "systemctl status nginx"

# 6. Generate a DB connection URL for your GUI
spyro proxy-url --profile staging | pbcopy

# 7. Stop tunnels
spyro down
```

## Configuration

Create a `spyro.toml` in your project root (or any parent directory — Spyro walks up to find it):

```toml
[profiles.staging]
host = "staging.example.com"
user = "deploy"
port = 22
# key = "~/.ssh/id_ed25519"
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [33060, 63790]

[profiles.staging.db]
host = "127.0.0.1"
port = 33060
name = "app_staging"
user = "forge"
password = ""
driver = "mysql"

[profiles.production]
host = "production.example.com"
user = "deploy"
port = 22
remote_path = "/var/www/app"
artisan = true
sudo = false
forwarded_ports = [33061]

[profiles.production.db]
host = "127.0.0.1"
port = 33061
name = "app_production"
user = "forge"
password = ""
driver = "mysql"
```

### Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | *required* | Remote server hostname or IP |
| `user` | string | `deploy` | SSH username |
| `port` | int | `22` | SSH port |
| `key` | string | `""` | Path to SSH private key |
| `remote_path` | string | `/var/www` | Working directory on remote server |
| `forwarded_ports` | list[int] | `[]` | Remote ports to forward locally |
| `artisan` | bool | `false` | Detect Laravel artisan on remote |
| `wordpress` | bool | `false` | Detect WordPress / WP-CLI on remote |
| `wp_cli_path` | string | `""` | Custom path to WP-CLI binary |
| `sudo` | bool | `false` | Enable JIT sudo escalation |
| `env_files` | list[str] | `[".env"]` | Remote env files to scan for credentials |

### Database Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `127.0.0.1` | Database host (usually localhost via tunnel) |
| `port` | int | `3306` | Database port |
| `name` | string | `""` | Database name |
| `user` | string | `""` | Database user |
| `password` | string | `""` | Database password (or leave empty for auto-detection) |
| `driver` | string | `mysql` | `mysql`, `postgres`, or `sqlite` |

### WordPress Profile

```toml
[profiles.wordpress]
host = "wp.example.com"
user = "deploy"
remote_path = "/var/www/html"
wordpress = true
sudo = false
forwarded_ports = [33062]

[profiles.wordpress.db]
host = "127.0.0.1"
port = 33062
name = "wordpress"
user = "wp_user"
password = ""
driver = "mysql"
```

### Credential Resolution

Spyro uses a dual-strategy approach:

1. **Explicit TOML definition** — If `password` is set in `spyro.toml`, use it
2. **Automatic detection fallback** — If empty, scan remote `.env`/config files for `DB_*` variables

## Commands

### Tunnel Management

| Command | Description |
|---------|-------------|
| `spyro up [profile]` | Start tunnels. Omit profile to start all. Runs as daemon by default. |
| `spyro down [profile]` | Stop tunnels and clean up process trees. |
| `spyro status [profile]` | Show health, active tunnels, PID, and port mappings. |
| `spyro logs [profile]` | Stream supervisor logs. Use `-f` to follow. |

### Remote Execution

| Command | Description |
|---------|-------------|
| `spyro run --all "cmd"` | Execute command across all profiles concurrently. |
| `spyro run -p staging "cmd"` | Execute on specific profile(s). |
| `spyro artisan <cmd>` | Run Laravel Artisan on remote host with auto-sudo. |
| `spyro wp <cmd>` | Run WP-CLI on remote host with auto-sudo. |
| `spyro cp /local/file /remote/path -p staging` | Upload local file to remote host. |
| `spyro cp :/remote/file /local/path -p staging` | Download remote file to local machine. Prefix remote paths with `:` for downloads. |

### Database Tools

| Command | Description |
|---------|-------------|
| `spyro db tunnel -p staging` | Start tunnel and print connection URL. |
| `spyro db shell -p staging` | Launch pre-authenticated `mysql` or `psql` client. |
| `spyro proxy-url -p staging` | Generate connection string for GUI tools (copies to clipboard with `\| pbcopy`). |

### Diagnostics

| Command | Description |
|---------|-------------|
| `spyro doctor` | Automated audit: SSH, paths, ports, artisan, WordPress, Redis, Supervisor, PHP-FPM, Node.js. |
| `spyro init` | Bootstrap `spyro.toml` and run toolchain audit. |
| `spyro pull-env -p staging` | Mirror remote `.env` to local `.env.remote`. |

### File Sync

| Command | Description |
|---------|-------------|
| `spyro pin ./src /var/www/app/src -p staging` | Pin a local directory for automatic sync. Auto-detects framework and applies exclusions. |
| `spyro pins` | List all pinned sync directories. |
| `spyro unpin ./src -p staging` | Remove a pinned directory. |
| `spyro sync -p staging` | Watch all pinned dirs and auto-sync on save. Use `--dry-run` to preview. |
| `spyro watch ./src /var/www/app/src -p staging` | Legacy manual sync (use `spyro pin` + `spyro sync` instead). |

## Architecture

```
src/
├── cli/            # Click CLI entry point and command implementations
│   ├── main.py     # Group registration, version, logging setup
│   └── commands.py # All 19 command functions
├── core/           # SSH handshake and database logic
│   ├── pty_engine.py  # PTY-based secure handshake engine
│   └── db.py          # Dual-strategy credential resolution
├── supervisor/     # Tunnel lifecycle and state management
│   ├── tunnel.py   # TunnelManager + STS supervisor (psutil-integrated)
│   └── state.py    # ~/.spyro/tunnels.json state store
├── security/       # Security boundaries
│   ├── ansi.py     # ANSI escape sequence sanitization
│   └── memory.py   # SecureCredential / SecureString (bytearray zeroing)
└── utils/          # Shared utilities
    ├── config.py   # TOML parsing, config discovery (walk-up)
    ├── keychain.py  # Native OS keychain via keyring
    └── paths.py    # Shell quoting, path helpers
```

### PTY Engine

The PTY engine (`src/core/pty_engine.py`) spawns native `ssh` in a pseudo-terminal using `pty.openpty()` and `os.fork()`. It:

- Reads stdout/stderr byte-by-byte
- Matches authentication prompts via regex
- Injects credentials directly into the PTY buffer
- Wraps credentials in `SecureCredential` for memory zeroing
- Sanitizes all output through the ANSI filter before printing

### Tunnel Supervisor (STS)

The STS (`src/supervisor/tunnel.py`) replaces `autossh` with a Python-native supervisor that:

- Monitors tunnel health via process liveness and port connectivity
- Restarts failed tunnels with exponential backoff (1s to 5min)
- Handles network roaming and sleep/wake cycles
- Uses `psutil` for cross-platform process tree management
- Tracks PIDs/PGIDs in `~/.spyro/tunnels.json` for orphan cleanup

### Service Detection

`spyro doctor` auto-detects these remote services:

| Service | Detection Method |
|---------|-----------------|
| Redis | `redis-server` binary, process check, `redis-cli info` |
| Supervisor | `supervisorctl` binary, `supervisord` process, managed process counts |
| PHP-FPM | `php-fpm*` binary (version-aware), `php-fpm -tt` pool count |
| Node.js | `node` binary, version, running processes |
| npm | `npm` binary, version |

### Smart Sync

The sync system (`spyro pin` / `spyro sync`) excludes sensitive files by default:

**Always excluded** (all frameworks):
- `.env*` — all environment files
- `*.local` — local config overrides
- `node_modules/`, `vendor/`, `__pycache__/`
- `*.swp`, `*~`, `.DS_Store`, `*.log`

**Framework-specific** (auto-detected or manual):
- **Laravel**: `.env`, `storage/logs/`, `bootstrap/cache/`, `vendor/`, `node_modules/`
- **WordPress**: `.env`, `wp-config.php`, `wp-content/cache/`, `vendor/`
- **Node.js**: `.env.local`, `.env.*.local`, `node_modules/`, `.next/`, `dist/`
- **Python**: `.env`, `__pycache__/`, `.venv/`, `*.pyc`

### Security Model

| Concern | Mitigation |
|---------|------------|
| Credential exposure | `SecureCredential` wraps passwords in `bytearray`, zeros with triple-pass (zero → random → zero) after use |
| Terminal injection | `sanitize_output()` strips all ANSI/OSC/DCS/C0 sequences, null bytes, BEL, and BS before printing |
| Shell injection | All user input passed through `shlex.quote()` |
| Config file permissions | Enforces `0600` on `spyro.toml` if it contains passwords |
| Keychain storage | Uses `keyring` library for native OS secure stores (macOS Keychain, Linux Secret Service) |
| Process isolation | PTY credentials are read into local variables and zeroed immediately after the interaction loop |

## Testing

```bash
# Unit tests (89 tests)
python3 -m pytest tests/unit/ -v

# Security tests — ANSI attack vectors (20 vectors)
python3 tests/security/test_ansi_attacks.py

# Security tests — Memory zeroing (8 tests)
python3 tests/security/test_memory_zeroing.py

# Phase 1 PoC — PTY engine validation
python3 tests/poc/test_pty_engine.py

# All tests including integration
python3 -m pytest tests/ -v
```

## Development

```bash
# Clone and install
git clone <repo-url> && cd spyro
pip install -e ".[dev]"

# Run tests
python3 -m pytest tests/unit/ -v

# Run full suite
python3 -m pytest tests/ -v

# Run security suite
python3 tests/security/test_ansi_attacks.py
python3 tests/security/test_memory_zeroing.py
```

## Platform Support

POSIX-compliant systems only:

- macOS (native)
- Linux (native)
- Windows Subsystem for Linux (WSL)

## License

MIT
