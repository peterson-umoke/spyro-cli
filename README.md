# Spyro

Intelligent SSH tunneling and remote command CLI for developers.

Spyro automates SSH port-forwarding, remote command execution, database credential resolution, and file synchronization through a declarative `spyro.toml` configuration. It replaces manual `ssh -L` coordination, `scp` routines, and `autossh` daemons with a single tool.

## Why Spyro

Developers working with remote servers spend significant time on repetitive SSH boilerplate: setting up port forwards, copying `.env` files, running artisan commands, syncing code. Spyro eliminates this by:

- **Automating tunnels**: one command brings up every forwarded port (authenticating with your keychain password, like every other command), and `spyro status` tells you when one has died
- **Resolving credentials** from either local TOML config or remote `.env`/Rails config files
- **Running remote commands** through a PTY engine that handles sudo escalation without leaking passwords
- **Syncing files** in real-time using native OS filesystem watchers

### The problem

Every developer working with remote servers knows this workflow:

```bash
# Terminal 1: SSH tunnel for MySQL
ssh -L 3306:localhost:3306 deploy@staging.example.com -N &

# Terminal 2: SSH tunnel for Redis
ssh -L 6379:localhost:6379 deploy@staging.example.com -N &

# Terminal 3: Run migrations
ssh deploy@staging.example.com "cd /var/www/app && php artisan migrate"

# Terminal 4: Check queue workers
ssh deploy@staging.example.com "sudo supervisorctl status"

# Terminal 5: Tail logs
ssh deploy@staging.example.com "tail -f /var/www/app/storage/logs/laravel.log"

# Terminal 6: Push a file
scp .env deploy@staging.example.com:/var/www/app/.env

# Oh no, the tunnel died. Restart it.
# Wait, which port was that tunnel on?
# Did I use -N or -f?
# What's the DB password again?
```

Five terminal tabs. Three forgotten port numbers. One crashed tunnel. And you haven't written a line of code yet.

### The solution

```bash
# One command to start all tunnels
spyro up staging

# Run anything on the remote server
spyro artisan migrate -p staging
spyro supervisor status -p staging
spyro logs laravel -p staging -f

# Push files
spyro cp .env :/var/www/app/.env -p staging

# One command to stop everything
spyro down
```

One config file. One CLI. Zero terminal tabs. Passwords stored in your OS keychain, never in config files.

## Installation

### macOS / Linux (recommended)

```bash
# Install with uv (fast Python package manager)
uv tool install git+https://github.com/peterson-umoke/spyro-cli.git

# Verify installation
spyro --version
spyro --help
```

### From source (for contributors)

```bash
git clone https://github.com/peterson-umoke/spyro-cli.git
cd spyro-cli

# Create virtual environment and install
uv sync

# Run without installing globally
uv run spyro --version

# Or install globally for daily use
uv tool install .

# With filesystem watcher support (for spyro sync/watch)
uv tool install . --with watchdog
```

### Update

```bash
# Check for updates and install the latest version
spyro update

# Only check, don't install
spyro update --check

# Force reinstall even if already up to date
spyro update --force
```

Spyro checks the GitHub releases API for the latest tag and reinstalls itself via ``uv tool install --reinstall`` from the canonical git URL (works whether installed with `uv` or `pip`; `git` must be on your PATH). After a command, an interactive terminal is told on stderr when a newer release exists; the check is cached for 24h and never touches `--json` output.

### Verify installation

```bash
spyro --version
spyro doctor
```

`spyro doctor` runs a full audit: checks SSH connectivity, remote paths, port availability, and detects all running services (Redis, PHP-FPM, Supervisor, Nginx/Caddy, etc.).

## Getting Started

### Step 1: Create your config

```bash
cd ~/Projects/my-laravel-app
spyro init
```

This creates `spyro.toml` in your project root. Edit it with your server details:

```toml
[profiles.staging]
host = "staging.example.com"
user = "deploy"
port = 22
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [3306, 6379]

[profiles.staging.db]
host = "127.0.0.1"
port = 3306
name = "myapp_staging"
user = "forge"
password = ""
driver = "mysql"
```

### Step 2: Store your password

```bash
spyro auth set -p staging
# Enter password when prompted — stored in your OS keychain
```

You'll never need to enter it again. Spyro reads it from macOS Keychain (or Linux Secret Service) automatically.

### Step 3: Start working

```bash
# Start database + Redis tunnels
spyro up staging

# Run migrations
spyro artisan migrate -p staging

# Open a MySQL shell
spyro db shell -p staging

# Check queue workers
spyro supervisor status -p staging

# When done, stop all tunnels
spyro down
```

That's the entire workflow. No more terminal tabs, no more port forwarding scripts, no more "what's the DB password?"

## Configuration

### Profile basics

Every server connection is a "profile" in `spyro.toml`:

```toml
[profiles.staging]
host = "staging.example.com"     # Server IP or hostname (required)
user = "deploy"                  # SSH username (required)
port = 22                        # SSH port (default: 22)
key = "~/.ssh/id_ed25519"       # SSH key (optional, uses default if empty)
remote_path = "/var/www/app"     # Where `spyro ssh` starts and relative `spyro cp` paths resolve
artisan = true                   # Enable Laravel artisan commands
wordpress = false                # Enable WordPress/WP-CLI commands
sudo = true                      # Allow sudo when needed
sudo_user = "www-data"           # Run artisan/tinker/wp as this user (default: root)
forwarded_ports = [3306, 6379]   # Remote ports to tunnel to localhost
env_files = [".env"]             # Remote env files to scan for DB credentials
```

### Configuration fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | *required* | Server hostname or IP address |
| `user` | string | `deploy` | SSH username |
| `port` | int | `22` | SSH port |
| `key` | string | `""` | Path to SSH private key (uses system default if empty) |
| `remote_path` | string | `/var/www` | Working directory on the server (also used by `artisan`, `run -C`, ...). `spyro ssh` starts here and relative remote paths in `spyro cp` resolve against it, but only when you set it in `spyro.toml`; `--home` uses your home directory instead |
| `forwarded_ports` | list[int] | `[]` | Remote ports to tunnel to localhost. The local port is the same unless it is taken, then the next free one is used |
| `artisan` | bool | `false` | Enable `spyro artisan` commands |
| `wordpress` | bool | `false` | Enable `spyro wp` commands |
| `sudo` | bool | `false` | Enable sudo escalation for commands |
| `sudo_user` | string | `""` | Run app commands (`artisan`, `tinker`, `eval`, `script`, `wp`) as this user via `sudo -u`. Default is root, which leaves root-owned files in `storage/` that the web server can no longer write |
| `env_files` | list[str] | `[".env"]` | Remote env files to scan for DB credentials |

### Database configuration

```toml
[profiles.staging.db]
host = "127.0.0.1"    # Always localhost (traffic goes through tunnel)
port = 3306           # The remote DB port; must be one of forwarded_ports
name = "myapp_staging"
user = "forge"
password = ""         # Leave empty = auto-detect from remote .env
driver = "mysql"      # mysql, postgres, or sqlite
```

**Credential resolution** (dual-strategy):

1. **Explicit** — If `password` is set in `spyro.toml`, use it
2. **Auto-detect** — If empty, read the remote `.env` / config files for `DB_*` variables (Laravel `pgsql` is mapped to `postgres`)

### Multiple users, same server

Different services running as different system users? Create a profile per user:

```toml
[profiles.app-api]
host = "192.168.1.100"
user = "deploy"
remote_path = "/var/www/api/current"
artisan = true
sudo = true

[profiles.app-ird]
host = "192.168.1.100"
user = "app-ird"
remote_path = "/home/app-ird/uploads"
sudo = false

[profiles.app-recharge]
host = "192.168.1.100"
user = "app-recharge"
remote_path = "/var/www/recharge/current"
sudo = false
```

Each profile gets its own credential:

```bash
spyro auth set -p app-api -w 'api-password'
spyro auth set -p app-ird -w 'ird-password'
spyro auth set -p app-recharge -w 'recharge-password'
```

Use them independently:

```bash
spyro artisan migrate:status -p app-api
spyro artisan tinker -p app-ird
spyro run "ls -la" -p app-recharge
```

### Multiple servers

```toml
[profiles.staging]
host = "10.0.0.1"
user = "deploy"
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [3306]

[profiles.production]
host = "10.0.0.2"
user = "deploy"
remote_path = "/var/www/app"
artisan = true
sudo = true
forwarded_ports = [3306]
```

```bash
spyro artisan migrate -p staging     # staging only
spyro artisan migrate -p production  # production only
spyro run "uptime" --all             # read-only commands can fan out to every profile
```

### WordPress profile

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

### Config file location

Spyro walks up from your current directory looking for `spyro.toml`. Put it in your project root and run commands from anywhere inside the project tree.

```
~/Projects/my-app/
├── spyro.toml          ← Spyro finds this
├── app/
│   ├── Http/
│   └── Models/
├── config/
└── routes/
```

```bash
cd ~/Projects/my-app
spyro artisan migrate -p staging    # Works

cd ~/Projects/my-app/app/Http
spyro artisan migrate -p staging    # Still works — walks up to find spyro.toml
```

## Credentials

### How it works

Spyro stores **one password per profile** in your OS keychain (macOS Keychain / Linux Secret Service). This single password is used for both SSH login and sudo — because in practice, they're the same.

If no keychain entry exists, Spyro prompts you interactively and stores it for next time. On hosts with no OS keychain (headless Linux, CI) set `SPYRO_PASSWORD_<PROFILE>` (profile name upper-cased, non-word characters as `_`) or `SPYRO_PASSWORD`; with no stored password and no terminal, Spyro does not prompt.

If SSH rejects the stored password, Spyro stops immediately and tells you to run `spyro auth set -p <profile> -f`.

### Authentication commands

```bash
# Store credentials (prompts securely)
spyro auth set -p staging

# Store non-interactively (for scripts/CI)
spyro auth set -p staging -w 'my-password'

# Overwrite existing without prompting
spyro auth set -p staging -w 'new-password' -f

# List all stored credentials
spyro auth list

# Delete a credential
spyro auth delete -p staging
```

### What happens when you run a command

```
spyro caddy restart -p dev

1. Spyro checks keychain for dev credential
2. Found → uses it for SSH login
3. Command contains "sudo" → checks if profile has sudo=true
4. sudo=true → uses same credential for sudo prompt
5. sudo=false → prints clear error: "User 'deploy' does not have sudo access"
6. Command runs, credentials zeroed from memory immediately after
```

### Security

- Passwords never leave your OS keychain
- Never stored in `spyro.toml` or any config file
- Wrapped in `SecureCredential` (bytearray) during use and zeroed after (best effort: Python cannot wipe every copy of a string)
- All output sanitized against terminal injection attacks
- Shell arguments passed through `shlex.quote()` to prevent injection
- Database passwords go to local clients via `MYSQL_PWD`/`PGPASSWORD` and to `mysqldump` over stdin, never on a command line

## Commands Reference

### Self-Update

```bash
spyro update                    # Check for updates and install
spyro update --check            # Only check, don't install
spyro update --force            # Reinstall even if already up to date
```
### Tunnel Management

```bash
spyro up                         # Start all tunnels (daemon mode)
spyro up staging                 # Start staging tunnel only
spyro up staging production      # Start multiple tunnels
spyro down                       # Stop all tunnels
spyro down staging               # Stop staging tunnel
spyro status                     # Show all active tunnels + health
spyro status staging             # Show staging tunnel details
```

**Tunnel behavior:**
- Runs as a background `ssh` (survives terminal close); `spyro up` authenticates with your keychain password and only reports success once the forwarded ports accept connections, otherwise it prints ssh's own error and exits 1
- `--no-daemon` keeps it in the foreground (Ctrl+C, `kill` or closing the terminal stops it)
- Not self-healing: if the connection drops, `spyro status` shows it as `stale` and `spyro up` (or any `db` command) starts a fresh one
- State in `~/.spyro/tunnels.json`; `spyro down` stops a tunnel through its ssh control socket and only ever signals a PID that is still an ssh/spyro process

### Laravel Artisan

```bash
spyro artisan <command> -p <profile>

# Examples:
spyro artisan migrate -p staging
spyro artisan migrate:status -p staging
spyro artisan queue:status -p staging
spyro artisan config:cache -p staging
spyro artisan route:cache -p staging
spyro artisan view:cache -p staging
spyro artisan optimize:clear -p staging
spyro artisan about -p staging
spyro artisan schedule:list -p staging
```

**Tinker (interactive REPL):**

```bash
spyro tinker -p staging                          # Interactive shell
spyro tinker -p staging -e "User::count()"       # One-shot eval
spyro tinker -p staging -f script.php            # Run a PHP file
```

**Eval (reliable one-shot PHP evaluation):**

```bash
# Evaluate any PHP expression via Laravel bootstrap (not PsySH)
# Avoids the quoting and output-capture issues of spyro tinker -e
spyro eval 'User::count()' -p staging
spyro eval 'DB::table("users")->count()' -p staging --json
spyro eval \"\\App\\Models\\User::first()->toArray()\" -p staging
```

The `eval` command works by generating a temporary PHP script that boots Laravel via `bootstrap/app.php`, evaluates your expression directly with `php` CLI, and echoes the result. No PsySH, no quote-nesting nightmares, no swallowed output.

### Database

```bash
# Tunnel + connection
spyro db tunnel -p staging              # Start tunnel, print connection URL
spyro db shell -p staging               # Open MySQL/MariaDB/psql prompt (password via environment)
spyro db ping -p staging                # Test connectivity

# Querying
spyro db query "SELECT COUNT(*) FROM users" -p staging
spyro db query "SHOW TABLES" -p staging

# Dumping
spyro db dump -p staging                           # Full dump
spyro db dump -p staging -t users,posts            # Specific tables
spyro db dump -p staging -t users -w "id > 100"   # With WHERE filter
spyro db dump -p staging -z                        # Gzip compressed
spyro db dump -p staging -d                        # Schema only (no data)
spyro db dump -p staging -o backup.sql             # Streams raw bytes; written atomically, owner-only (0600)

# Listing
spyro db list-databases -p staging                 # List all databases

# GUI tools
spyro proxy-url -p staging              # Generate connection string
spyro proxy-url -p staging | pbcopy     # Copy to clipboard
```

**Proxy URL output:**
```
mysql://forge:@127.0.0.1:3306/myapp_staging
```

Paste this into TablePlus, Sequel Ace, DBeaver, or any database GUI.

### Service Management

**Supervisor (queue workers, Reverb, etc.):**

```bash
spyro supervisor status -p staging                    # All processes
spyro supervisor restart -p staging                   # Restart all
spyro supervisor restart laravel-queue -p staging     # Restart specific
spyro supervisor tail laravel-reverb -p staging       # Tail stderr logs
```

**Redis:**

```bash
spyro redis ping -p staging                    # Test connection
spyro redis stats -p staging                   # Connections, commands/s, keyspace
spyro redis info -p staging -s server          # Server info section
spyro redis info -p staging -s memory          # Memory info
spyro redis cli DBSIZE -p staging              # Run arbitrary redis-cli command
spyro redis cli KEYS "*" -p staging            # List all keys
```

**PHP:**

```bash
spyro php version -p staging                   # PHP version
spyro php extensions -p staging                # List loaded extensions
spyro php extensions -p staging --filter pdo   # Filter extensions
spyro php info -p staging --option memory_limit  # PHP config value
spyro php fpm-status -p staging                # PHP-FPM pool status
spyro php restart -p staging                   # Restart PHP-FPM
```

**Web servers:**

```bash
# Caddy
spyro caddy version -p staging
spyro caddy status -p staging
spyro caddy restart -p staging

# Nginx
spyro nginx version -p staging
spyro nginx status -p staging
spyro nginx sites -p staging
spyro nginx restart -p staging

# Apache
spyro apache version -p staging
spyro apache modules -p staging
spyro apache sites -p staging
spyro apache status -p staging
spyro apache restart -p staging
```

### Remote Commands

```bash
# Run any command
spyro run "df -h" -p staging
spyro run "cat /var/log/syslog | tail -50" -p staging
spyro run "du -sh /var/www/app/storage" -p staging

# Run across all environments
spyro run "uptime" --all
spyro run "free -m" --all

# Commands time out after 60s by default (a timeout is reported, and the remote
# command may keep running). Raise it per command or globally:
spyro artisan migrate -p staging --timeout 600      # or [defaults] command_timeout = 600
```

### Interactive Shell

```bash
# Open an interactive SSH session for a profile.
# It starts in the profile's remote_path (when spyro.toml sets one)
spyro ssh -p staging
spyro shell -p staging       # Alias for ssh

# Start in your home directory (where plain ssh lands) instead
spyro ssh -p staging --home  # --root is the same flag

# Auto-detects profile if only one is configured
spyro ssh

# Once connected, you're in a full interactive shell:
#   - Colors, tab completion, vim, htop — all pass through
#   - Ctrl+C, arrows, history work normally
#   - Password or key-based auth — both handled automatically
# Exit with Ctrl+D or type "exit"
```

If `remote_path` does not exist on the server you still get a shell, in your home directory, with a notice. For a Capistrano layout set `remote_path` to the `current` symlink (e.g. `/var/www/app/current`) to land in the live release.

**How it works:** Uses the same PTY engine as `spyro run` to inject credentials from the OS keychain during auth, then hands over to a raw terminal relay. Handles both password-based and key-based SSH authentication. After auth, credentials are zeroed from memory — the interactive session has no access to them.

### Process Listing

```bash
# List all processes on remote server
spyro ps -p staging

# Filter with grep
spyro ps -p staging --grep php
spyro ps -p staging -g nginx

# JSON output for scripting
spyro ps -p staging --json
```

### Configuration Management

```bash
# Validate spyro.toml schema
spyro config validate
spyro cfg validate          # alias

# Checks:
#   - Required fields (host, user)
#   - Port ranges (1-65535)
#   - SSH key file existence
#   - Duplicate forwarded ports
#   - SSH config Host matching
```

### Default Profile

Set a default profile in `spyro.toml` so you can omit `-p`:

```toml
[defaults]
profile = "staging"
```

Now `spyro ssh`, `spyro artisan migrate`, etc. will use `staging` by default.

### SSH Config Integration

Profiles automatically inherit settings from `~/.ssh/config` Host blocks. If a profile
name matches an SSH Host entry, HostName/User/Port/IdentityFile are applied.

### Machine-Readable Output

Several commands support `--json` for CI/CD or script consumption:

```bash
spyro status --json        # Tunnel states as JSON
spyro doctor --json        # Diagnostic results as JSON
spyro pins --json          # Pinned sync dirs as JSON
spyro ps --json            # Process list as JSON (columns parsed)
```

### Shell Completion

```bash
# Show completion script for your shell
spyro --show-completion

# Get install command to add to your ~/.zshrc / ~/.bashrc
spyro --install-completion
```

### File Transfer

```bash
# Upload local file to remote
spyro cp ./README.md :/var/www/app/README.md -p staging
spyro upload ./README.md :/var/www/app/README.md -p staging   # alias

# Relative remote paths start in the profile's remote_path (when spyro.toml sets one)
spyro cp .env :.env -p staging                 # -> <remote_path>/.env
spyro cp .env :storage/ -p staging             # -> <remote_path>/storage/
spyro cp .env :.env -p staging --home          # -> ~/.env (your home directory; --root is the same flag)
spyro cp :storage/logs/laravel.log ./laravel.log -p staging   # downloads resolve the same way

# Download remote file to local
spyro cp :/var/www/app/.env ./.env.remote -p staging

# Upload entire directory
spyro cp -r ./public :/var/www/app/public -p staging

# Upload to multiple profiles (comma-separated)
spyro cp ./config.php :/var/www/config.php -p staging,dev

# Upload to multiple profiles (repeat -p)
spyro cp ./config.php :/var/www/config.php -p staging -p dev

# Upload to all profiles
spyro cp ./config.php :/var/www/config.php --all

# Upload to all profiles except specific ones
spyro cp ./config.php :/var/www/config.php --all --except ird-server,production
```

`--timeout N` sets the per-profile transfer limit (default 120s). `--parents` works with the `:/remote/path` form.

**Path convention:**
- Local paths: `/path/to/file`
- Remote paths: `:` prefix → `:/remote/path` (absolute, used as given) or `:relative/path` (relative to the profile's `remote_path`, or to your home directory with `--home`; `~/x` is always relative to your home directory)
- Profile: `-p name`, multiple `-p`, comma-separated, `--all`, or `--all --except`

### Environment

```bash
# Pull remote .env to local file
spyro env pull -p staging                        # Creates .env.remote
spyro env pull -p staging --dest .env.staging    # Custom output path

# Compare local .env with remote
spyro env diff -p staging                        # Unified diff of keys; values are never printed

# Push local .env to remote
spyro env push -p staging                        # Uploads .env
spyro env push -p staging custom.env             # Upload custom file

# Legacy alias (still works)
spyro pull-env -p staging
```

### Logs

```bash
spyro logs laravel -p staging -n 100       # Last 100 lines
spyro logs laravel -p staging -f           # Follow until Ctrl+C (no timeout)
spyro logs nginx -p staging                # Nginx access log
spyro logs nginx-error -p staging          # Nginx error log
spyro logs apache -p staging               # Apache access log
spyro logs php -p staging                  # PHP-FPM error log
spyro logs supervisor staging              # Spyro's own tunnel log for a profile
```

### Diagnostics

```bash
spyro doctor                    # Full audit of all profiles

# Output:
# 1. SSH connectivity
#    ✓ staging: reachable
#    ✓ production: reachable
#
# 2. Remote path validity
#    ✓ staging: /var/www/app/current exists
#    ✓ production: /var/www/app/current exists
#
# 3. Local port conflicts
#    ✓ Port 3306: available
#    ✓ Port 6379: available
#
# 4. Laravel artisan detection
#    ✓ staging: artisan found
#    ✓ production: artisan found
#
# 6. Remote services
#    staging (10.0.0.1)
#      ✓ Redis
#      ✓ Supervisor (3 managed)
#      ✓ PHP-FPM (8.3)
#      ✓ Caddy
```

## Tips & Tricks

### Use `-p` everywhere

Almost every command takes `-p <profile>`. Make it muscle memory:

```bash
spyro artisan migrate -p staging
spyro db shell -p staging
spyro caddy restart -p staging
spyro logs laravel -p staging -f
```

### Quick DB access

```bash
# One-liner: open MySQL shell
spyro db shell -p staging

# Or get connection string for GUI tools
spyro proxy-url -p staging | pbcopy
# Paste into TablePlus/Sequel Ace/DBeaver
```

### Check before you deploy

```bash
spyro doctor                           # Full audit
spyro supervisor status -p staging     # Queue workers healthy?
spyro redis ping -p staging            # Redis alive?
spyro db ping -p staging               # Database reachable?
spyro artisan about -p staging         # App boots correctly?
```

### Run across all environments

```bash
spyro run "uptime" --all
spyro run "df -h" --all
```

### Non-interactive auth (CI/CD)

```bash
# Headless hosts have no OS keychain: pass the password through the environment
export SPYRO_PASSWORD_STAGING="$STAGING_PASSWORD"     # or SPYRO_PASSWORD for every profile

# Use in GitHub Actions
- name: Deploy check
  env:
    SPYRO_PASSWORD_STAGING: ${{ secrets.STAGING_SSH_PASSWORD }}
  run: spyro run "uptime" -p staging
```

### Shell aliases

Add to `~/.zshrc` or `~/.bashrc`:

```bash
alias su='spyro up'
alias sd='spyro down'
alias ss='spyro status'
alias sa='spyro artisan'
alias sdsh='spyro db shell'
alias sdoc='spyro doctor'
```

### Debug tunnel issues

```bash
spyro status                    # What's running?
spyro logs supervisor staging    # Tunnel log
spyro doctor                    # Full audit
ssh deploy@staging.example.com  # Raw SSH fallback
```

### Multiple users on one server

```toml
[profiles.app-api]
host = "192.168.1.100"
user = "deploy"
remote_path = "/var/www/api/current"
sudo = true

[profiles.app-ird]
host = "192.168.1.100"
user = "app-ird"
remote_path = "/home/app-ird/uploads"
sudo = false
```

```bash
spyro auth set -p app-api -w 'password1'
spyro auth set -p app-ird -w 'password2'

spyro artisan migrate -p app-api
spyro artisan tinker -p app-ird
```

### When sudo isn't available

If a profile has `sudo = false` and you run a command that needs sudo:

```
✗ User 'deploy' does not have sudo access on staging
  Set sudo = true in your spyro.toml for this profile
```

Clear error, no cryptic failures.

### Config file location

Spyro walks up from your current directory. Put `spyro.toml` in your project root:

```bash
cd ~/Projects/my-app
spyro artisan migrate -p staging    # Works from project root

cd ~/Projects/my-app/app/Http
spyro artisan migrate -p staging    # Still works — walks up
```

## Architecture

```
spyro.toml              ← Your config (per-project, declarative)
    ↓
CLI (click)             ← Command routing + validation
    ↓
PTY Engine              ← SSH with pseudo-terminal (handles sudo prompts)
    │                     Spawns native ssh via pty.openpty() + os.fork()
    │                     Matches auth prompts via regex
    │                     Injects credentials into PTY buffer
    │                     Zeroes credentials after use
    ↓
Keychain (keyring)      ← Passwords in OS keychain (macOS/Linux native)
    ↓
Tunnels                 ← ssh -f -N -L via the PTY engine (password hosts work)
                          Verified before success is reported
                          State, PIDs and control socket in ~/.spyro/tunnels.json
```

### PTY Engine

The PTY engine (`src/spyro/core/pty_engine.py`) spawns native `ssh`/`scp` in a pseudo-terminal using `pty.openpty()` and `os.fork()`. It:

- Reads stdout/stderr byte-by-byte
- Handles both **password-based** and **key-based** SSH auth automatically
  - Password auth: matches the SSH prompt (`user@host's password:`, `Password:`) and injects the stored password into the PTY; prompts from programs on the server are never answered with it
  - A missing or rejected password fails immediately with a message instead of idling until the timeout
  - Key auth: detects shell output (MOTD, prompt) and skips directly to raw relay mode
- Wraps credentials in `SecureCredential` for memory zeroing
- Strips every terminal escape/control sequence from output before it is printed
- Sets the PTY window size and follows terminal resizes in interactive sessions
- Uses `~/.spyro/sockets/` (`ControlPath=.../%C`, a fixed-length hash) for connection sharing, which avoids macOS Unix socket path length limits

### Tunnels

`src/spyro/supervisor/tunnel.py` runs `ssh -f -N -L ...` through the PTY engine, so password hosts work, and lets ssh become its own ControlMaster. `ssh -f` only backgrounds once authentication and the forwards have succeeded. `src/spyro/supervisor/state.py` records the PID, local ports and control socket in `~/.spyro/tunnels.json` (locked and written atomically). There is no restart loop: a tunnel that dies shows as `stale`.

### Service Detection

`spyro doctor` auto-detects these remote services:

| Service | Detection Method |
|---------|-----------------|
| Redis | `redis-server` binary, process check, `redis-cli info` |
| Supervisor | `supervisorctl` binary, `supervisord` process, managed process counts |
| PHP-FPM | `php-fpm*` binary (version-aware), `php-fpm -tt` pool count |
| PHP | `php` binary, version, extension count, FPM pool children |
| Apache | `apache2`/`httpd` binary, version, module count |
| Nginx | `nginx` binary, version, worker processes, site count |
| Caddy | `caddy` binary, version, process count |
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
| Credential exposure | `SecureCredential` wraps passwords in `bytearray`, zeros with triple-pass (zero → random → zero) after use (best effort in Python) |
| Terminal injection | `strip_ansi()` strips all CSI/OSC/DCS/ESC sequences, C1 controls, NUL, BEL and BS from remote output and resolves carriage returns before it is printed; remote text is also never parsed as Rich markup |
| Shell injection | All user input passed through `shlex.quote()` |
| Config file permissions | Enforces `0600` on `spyro.toml` if it contains passwords |
| Keychain storage | Uses `keyring` library for native OS secure stores (macOS Keychain, Linux Secret Service) |
| Process isolation | PTY credentials are read into local variables and zeroed immediately after the interaction loop |

## Testing

```bash
uv sync --all-extras                    # pytest, pip-audit and watchdog are extras
uv run python -m pytest                 # everything (no network or SSH needed)
uv run python -m pytest tests/unit/test_config.py -k profile
uv run python tests/security/test_ansi_attacks.py     # security suites also run as scripts
```

## Platform Support

- **macOS** — native (Keychain integration)
- **Linux** — native (Secret Service / kwallet)
- **Windows** — WSL only

## Planned Features

### Deployment (Capistrano-style)

Zero-downtime deployment for Laravel, React, Angular, Next.js, and Vue.js projects.

```bash
spyro deploy -p staging              # Deploy to staging
spyro deploy -p staging --dry-run    # Preview what would happen
spyro rollback -p staging            # Rollback to previous release
```

**How it works:**

1. Sync local code → remote via rsync
2. Create timestamped release directory
3. Run build steps (composer install, npm build, etc.)
4. Swap symlink: `/var/www/app/current` → new release
5. Health check to verify deployment
6. Cleanup old releases (keeps last 5)
7. Auto-rollback on failure

**Configuration:**

```toml
[profiles.staging]
branch = "main"
deploy_strategy = "laravel"    # laravel, node, static, nextjs, vuejs

[profiles.staging.deploy]
keep_releases = 5
shared_dirs = ["storage", "bootstrap/cache"]
shared_files = [".env"]
health_check = "/health"
health_timeout = 30
post_deploy = [
    "php artisan migrate --force",
    "php artisan config:cache",
    "php artisan route:cache",
    "php artisan view:cache",
    "php artisan queue:restart",
]
```

**Project type strategies:**

| Strategy | Build Steps | Use Case |
|----------|-------------|----------|
| `laravel` | composer install, artisan migrate, artisan optimize | Laravel PHP apps |
| `node` | npm ci, npm run build | React, Vue, Angular |
| `static` | (none) | HTML/CSS/JS sites |
| `nextjs` | npm ci, npm run build, pm2 restart | Next.js apps |
| `vuejs` | npm ci, npm run build | Vue.js apps |

**Rollback:**

```bash
spyro rollback -p staging
# Swaps symlink to previous release
# Re-runs post-deploy hooks
```

**Health check:**

After deployment, Spyro verifies the app is healthy by hitting your health endpoint. If it fails, the deployment is rolled back automatically.

```toml
[profiles.staging.deploy]
health_check = "/health"    # HTTP endpoint to check
health_timeout = 30         # Seconds to wait
```

**Status:** Planned — implementation in progress. (Today `spyro deploy` is an alias for `spyro cp`.)

---

## License

MIT
