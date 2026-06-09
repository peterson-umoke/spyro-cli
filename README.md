# Spyro

**The lazy developer's SSH tool.** One config file, zero terminal tabs.

Spyro replaces manual `ssh -L`, `scp`, `autossh`, and "wait, which port was that tunnel on?" with a single CLI that handles tunnels, remote commands, database access, file sync, and service management — all from your project directory.

## What Spyro Does

| Problem | Spyro Solution |
|---------|---------------|
| `ssh -L 3306:localhost:3306 user@server &` × 5 servers | `spyro up` — tunnels auto-heal, survive sleep/wake |
| `ssh user@server "cd /var/www/app && php artisan migrate"` | `spyro artisan migrate -p staging` |
| "What's the DB password for staging?" | Stored in your OS keychain, auto-resolved |
| `scp .env user@server:/var/www/app/.env` | `spyro pull-env -p staging` |
| "Is the queue worker running?" | `spyro supervisor status -p staging` |
| Manual `supervisorctl restart` via SSH | `spyro supervisor restart -p staging` |

## Installation

### macOS / Linux (recommended)

```bash
# Install with uv (fast Python package manager)
uv tool install git+https://github.com/peterson-umoke/spyro-cli.git

# Verify
spyro --version
```

### From source

```bash
git clone https://github.com/peterson-umoke/spyro-cli.git
cd spyro-cli
uv sync
uv tool install .
```

### Update

```bash
uv tool install --force git+https://github.com/peterson-umoke/spyro-cli.git
```

## Getting Started

### 1. Create your config

```bash
cd ~/Projects/my-app
spyro init
```

This creates `spyro.toml` in your project root. Edit it:

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

### 2. Store your password

```bash
spyro auth set -p staging
# You'll be prompted once — stored in your OS keychain forever
```

### 3. Start working

```bash
spyro up staging              # Start DB tunnel
spyro artisan migrate -p staging   # Run migrations
spyro db shell -p staging     # Open MySQL prompt
spyro down                    # Stop tunnels when done
```

That's it. No more `ssh` tabs, no more port forwarding scripts.

## Configuration

### Profile basics

```toml
[profiles.myserver]
host = "192.168.1.100"         # Server IP or hostname
user = "deploy"                # SSH username
port = 22                      # SSH port (default: 22)
remote_path = "/var/www/app"   # Working directory on server
artisan = true                 # Enable Laravel artisan commands
sudo = true                    # Allow sudo when needed
forwarded_ports = [3306, 6379] # Ports to tunnel locally
```

### Database config

```toml
[profiles.myserver.db]
host = "127.0.0.1"    # Always localhost (via tunnel)
port = 3306           # Must match forwarded_ports
name = "myapp"
user = "forge"
password = ""         # Leave empty = auto-detect from remote .env
driver = "mysql"      # mysql, postgres, or sqlite
```

### Multiple users, same server

Different services running as different users? One profile per user:

```toml
[profiles.dev-api]
host = "34.250.32.252"
user = "peter.umoke"
remote_path = "/var/www/api/current"
sudo = true

[profiles.dev-ird]
host = "34.250.32.252"
user = "sftp-staging-ird"
remote_path = "/home/sftp-staging-ird/uploads"
sudo = false

[profiles.dev-recharge]
host = "34.250.32.252"
user = "recharge-user"
remote_path = "/var/www/recharge/current"
sudo = false
```

```bash
spyro auth set -p dev-api -w 'password1'
spyro auth set -p dev-ird -w 'password2'
spyro auth set -p dev-recharge -w 'password3'

spyro artisan migrate:status -p dev-api
spyro artisan tinker -p dev-ird
spyro run "ls -la" -p dev-recharge
```

### Multiple servers

```toml
[profiles.staging]
host = "10.0.0.1"
user = "deploy"
# ...

[profiles.production]
host = "10.0.0.2"
user = "deploy"
# ...
```

```bash
spyro artisan migrate -p staging     # staging only
spyro artisan migrate -p production  # production only
spyro artisan migrate --all          # both at once
```

### Config file location

Spyro walks up from your current directory looking for `spyro.toml`. Put it in your project root and run commands from anywhere inside the project.

## Credentials

### How it works

Spyro stores **one password per profile** in your OS keychain (macOS Keychain / Linux Secret Service). Same password for SSH login and sudo — because they're the same.

If no keychain entry exists, Spyro prompts you interactively and stores it for next time.

### Commands

```bash
# Store (prompts securely)
spyro auth set -p staging

# Store non-interactively
spyro auth set -p staging -w 'my-password'

# List what's stored
spyro auth list

# Delete
spyro auth delete -p staging
```

### Tips

- **One password, all commands.** Set it once with `spyro auth set`, never think about it again.
- **Different users = different profiles.** Each profile gets its own credential.
- **No password in config files.** `spyro.toml` never stores passwords — keychain does.

## Commands

### Tunnels

```bash
spyro up                     # Start all tunnels
spyro up staging             # Start staging tunnel
spyro down                   # Stop all tunnels
spyro down staging           # Stop staging tunnel
spyro status                 # Show all active tunnels
spyro status staging         # Show staging tunnel details
```

### Laravel Artisan

```bash
spyro artisan migrate -p staging
spyro artisan queue:status -p staging
spyro artisan config:cache -p staging
spyro artisan tinker -p staging          # Interactive REPL
spyro artisan tinker -p staging -e "User::count()"  # One-shot
```

### Database

```bash
spyro db tunnel -p staging              # Start tunnel, show connection URL
spyro db shell -p staging               # Open MySQL/MariaDB prompt
spyro db ping -p staging                # Test connection
spyro db query "SELECT COUNT(*) FROM users" -p staging
spyro db dump -p staging                # Full dump
spyro db dump -p staging -t users,posts # Specific tables
spyro db dump -p staging -z             # Gzipped
spyro proxy-url -p staging              # Connection string for GUI tools
```

### Services

```bash
# Supervisor (queue workers, reverb, etc.)
spyro supervisor status -p staging
spyro supervisor restart -p staging
spyro supervisor restart laravel-queue -p staging
spyro supervisor tail laravel-reverb -p staging

# Redis
spyro redis ping -p staging
spyro redis stats -p staging
spyro redis cli KEYS "*" -p staging

# PHP
spyro php version -p staging
spyro php restart -p staging

# Web servers
spyro caddy version -p staging
spyro caddy status -p staging
spyro caddy restart -p staging
spyro nginx restart -p staging
spyro apache restart -p staging
```

### Remote commands

```bash
spyro run "df -h" -p staging              # Run any command
spyro run "cat /var/log/syslog" -p staging
spyro cp ./README.md :/var/www/app/ -p staging  # Upload file
spyro cp :/var/www/app/.env ./              # Download file
```

### Environment

```bash
spyro pull-env -p staging    # Copy remote .env to local .env.remote
```

### Logs

```bash
spyro logs laravel -p staging -n 100    # Last 100 lines
spyro logs laravel -p staging -f        # Follow (tail -f)
spyro logs nginx -p staging
spyro logs php -p staging
```

### Diagnostics

```bash
spyro doctor                 # Full audit of all profiles
```

## Tips & Tricks

### Use `-p` everywhere

Almost every command takes `-p <profile>`. Make it a habit:

```bash
spyro artisan migrate -p staging
spyro db shell -p staging
spyro caddy restart -p staging
```

### Short aliases

Add these to your shell profile (`~/.zshrc` or `~/.bashrc`):

```bash
alias su='spyro up'
alias sd='spyro down'
alias ss='spyro status'
alias sa='spyro artisan'
```

### Quick DB access

```bash
# One-liner: tunnel + open shell
spyro db shell -p staging

# Or just get the connection string for TablePlus/Sequel Ace
spyro proxy-url -p staging | pbcopy
```

### Run across all environments

```bash
spyro artisan queue:status --all
spyro run "uptime" --all
```

### Check before you deploy

```bash
spyro doctor                  # Audit all profiles
spyro supervisor status -p staging  # Are queue workers healthy?
spyro redis ping -p staging         # Is Redis alive?
spyro db ping -p staging            # Can we reach the database?
```

### Non-interactive auth (CI/CD)

```bash
spyro auth set -p staging -w "$STAGING_PASSWORD" -f
```

### Debug tunnel issues

```bash
spyro status                  # See what's running
spyro logs -p staging         # Supervisor logs
ssh deploy@staging.example.com  # Raw SSH fallback
```

## Architecture

```
spyro.toml          ← Your config (per-project)
    ↓
CLI (click)         ← Command routing
    ↓
PTY Engine          ← SSH with pseudo-terminal (handles sudo prompts)
    ↓
Keychain (keyring)  ← Passwords stored in OS keychain
    ↓
Tunnel Supervisor   ← Self-healing SSH tunnels (survives sleep/wake)
```

## Testing

```bash
# Unit tests
python3 -m pytest tests/unit/ -v

# Security tests
python3 tests/security/test_ansi_attacks.py
python3 tests/security/test_memory_zeroing.py

# All tests
python3 -m pytest tests/ -v
```

## Platform Support

- macOS (native)
- Linux (native)
- Windows (WSL only)

## License

MIT
