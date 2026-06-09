1|# Spyro
2|
3|Intelligent SSH tunneling and remote command CLI for developers.
4|
5|Spyro automates SSH port-forwarding, remote command execution, database credential resolution, and file synchronization through a declarative `spyro.toml` configuration. It replaces manual `ssh -L` coordination, `scp` routines, and `autossh` daemons with a single, self-healing tool.
6|
7|## Why Spyro
8|
9|Developers working with remote servers spend significant time on repetitive SSH boilerplate: setting up port forwards, copying `.env` files, running artisan commands, syncing code. Spyro eliminates this by:
10|
11|- **Automating tunnels** with a self-healing supervisor that survives network drops, sleep/wake cycles, and process crashes
12|- **Resolving credentials** from either local TOML config or remote `.env`/Rails config files
13|- **Running remote commands** through a PTY engine that handles sudo escalation without leaking passwords
14|- **Syncing files** in real-time using native OS filesystem watchers
15|
16|### The problem
17|
18|Every developer working with remote servers knows this workflow:
19|
20|```bash
21|# Terminal 1: SSH tunnel for MySQL
22|ssh -L 3306:localhost:3306 deploy@staging.example.com -N &
23|
24|# Terminal 2: SSH tunnel for Redis
25|ssh -L 6379:localhost:6379 deploy@staging.example.com -N &
26|
27|# Terminal 3: Run migrations
28|ssh deploy@staging.example.com "cd /var/www/app && php artisan migrate"
29|
30|# Terminal 4: Check queue workers
31|ssh deploy@staging.example.com "sudo supervisorctl status"
32|
33|# Terminal 5: Tail logs
34|ssh deploy@staging.example.com "tail -f /var/www/app/storage/logs/laravel.log"
35|
36|# Terminal 6: Push a file
37|scp .env deploy@staging.example.com:/var/www/app/.env
38|
39|# Oh no, the tunnel died. Restart it.
40|# Wait, which port was that tunnel on?
41|# Did I use -N or -f?
42|# What's the DB password again?
43|```
44|
45|Five terminal tabs. Three forgotten port numbers. One crashed tunnel. And you haven't written a line of code yet.
46|
47|### The solution
48|
49|```bash
50|# One command to start all tunnels
51|spyro up staging
52|
53|# Run anything on the remote server
54|spyro artisan migrate -p staging
55|spyro supervisor status -p staging
56|spyro logs laravel -p staging -f
57|
58|# Push files
59|spyro cp .env :/var/www/app/.env -p staging
60|
61|# One command to stop everything
62|spyro down
63|```
64|
65|One config file. One CLI. Zero terminal tabs. Tunnels that heal themselves. Passwords stored in your OS keychain, never in config files.
66|
67|## Installation
68|
69|### macOS / Linux (recommended)
70|
71|```bash
72|# Install with uv (fast Python package manager)
73|uv tool install git+https://github.com/peterson-umoke/spyro-cli.git
74|
75|# Verify installation
76|spyro --version
77|spyro --help
78|```
79|
80|### From source (for contributors)
81|
82|```bash
83|git clone https://github.com/peterson-umoke/spyro-cli.git
84|cd spyro-cli
85|
86|# Create virtual environment and install
87|uv sync
88|
89|# Run without installing globally
90|uv run spyro --version
91|
92|# Or install globally for daily use
93|uv tool install .
94|
95|# With filesystem watcher support (for spyro sync/watch)
96|uv tool install . --with watchdog
97|```
98|
99|### Update
100|
101|```bash
102|uv tool install --force git+https://github.com/peterson-umoke/spyro-cli.git
103|```
104|
105|### Verify installation
106|
107|```bash
108|spyro --version
109|spyro doctor
110|```
111|
112|`spyro doctor` runs a full audit: checks SSH connectivity, remote paths, port availability, and detects all running services (Redis, PHP-FPM, Supervisor, Nginx/Caddy, etc.).
113|
114|## Getting Started
115|
116|### Step 1: Create your config
117|
118|```bash
119|cd ~/Projects/my-laravel-app
120|spyro init
121|```
122|
123|This creates `spyro.toml` in your project root. Edit it with your server details:
124|
125|```toml
126|[profiles.staging]
127|host = "staging.example.com"
128|user = "deploy"
129|port = 22
130|remote_path = "/var/www/app"
131|artisan = true
132|sudo = true
133|forwarded_ports = [3306, 6379]
134|
135|[profiles.staging.db]
136|host = "127.0.0.1"
137|port = 3306
138|name = "myapp_staging"
139|user = "forge"
140|password = ""
141|driver = "mysql"
142|```
143|
144|### Step 2: Store your password
145|
146|```bash
147|spyro auth set -p staging
148|# Enter password when prompted — stored in your OS keychain
149|```
150|
151|You'll never need to enter it again. Spyro reads it from macOS Keychain (or Linux Secret Service) automatically.
152|
153|### Step 3: Start working
154|
155|```bash
156|# Start database + Redis tunnels
157|spyro up staging
158|
159|# Run migrations
160|spyro artisan migrate -p staging
161|
162|# Open a MySQL shell
163|spyro db shell -p staging
164|
165|# Check queue workers
166|spyro supervisor status -p staging
167|
168|# When done, stop all tunnels
169|spyro down
170|```
171|
172|That's the entire workflow. No more terminal tabs, no more port forwarding scripts, no more "what's the DB password?"
173|
174|## Configuration
175|
176|### Profile basics
177|
178|Every server connection is a "profile" in `spyro.toml`:
179|
180|```toml
181|[profiles.staging]
182|host = "staging.example.com"     # Server IP or hostname (required)
183|user = "deploy"                  # SSH username (required)
184|port = 22                        # SSH port (default: 22)
185|key = "~/.ssh/id_ed25519"       # SSH key (optional, uses default if empty)
186|remote_path = "/var/www/app"     # Working directory on server (required)
187|artisan = true                   # Enable Laravel artisan commands
188|wordpress = false                # Enable WordPress/WP-CLI commands
189|sudo = true                      # Allow sudo when needed
190|forwarded_ports = [3306, 6379]   # Ports to tunnel locally
191|env_files = [".env"]             # Remote env files to scan for DB credentials
192|```
193|
194|### Configuration fields
195|
196|| Field | Type | Default | Description |
197||-------|------|---------|-------------|
198|| `host` | string | *required* | Server hostname or IP address |
199|| `user` | string | `deploy` | SSH username |
200|| `port` | int | `22` | SSH port |
201|| `key` | string | `""` | Path to SSH private key (uses system default if empty) |
202|| `remote_path` | string | `/var/www` | Working directory on remote server |
203|| `forwarded_ports` | list[int] | `[]` | Remote ports to tunnel to localhost |
204|| `artisan` | bool | `false` | Enable `spyro artisan` commands |
205|| `wordpress` | bool | `false` | Enable `spyro wp` commands |
206|| `sudo` | bool | `false` | Enable sudo escalation for commands |
207|| `env_files` | list[str] | `[".env"]` | Remote env files to scan for DB credentials |
208|
209|### Database configuration
210|
211|```toml
212|[profiles.staging.db]
213|host = "127.0.0.1"    # Always localhost (traffic goes through tunnel)
214|port = 3306           # Must match a port in forwarded_ports
215|name = "myapp_staging"
216|user = "forge"
217|password = ""         # Leave empty = auto-detect from remote .env
218|driver = "mysql"      # mysql, postgres, or sqlite
219|```
220|
221|**Credential resolution** (dual-strategy):
222|
223|1. **Explicit** — If `password` is set in `spyro.toml`, use it
224|2. **Auto-detect** — If empty, scan remote `.env` / config files for `DB_*` variables
225|
226|### Multiple users, same server
227|
228|Different services running as different system users? Create a profile per user:
229|
230|```toml
231|[profiles.app-api]
232|host = "192.168.1.100"
233|user = "deploy"
234|remote_path = "/var/www/api/current"
235|artisan = true
236|sudo = true
237|
238|[profiles.app-ird]
239|host = "192.168.1.100"
240|user = "app-ird"
241|remote_path = "/home/app-ird/uploads"
242|sudo = false
243|
244|[profiles.app-recharge]
245|host = "192.168.1.100"
246|user = "app-recharge"
247|remote_path = "/var/www/recharge/current"
248|sudo = false
249|```
250|
251|Each profile gets its own credential:
252|
253|```bash
254|spyro auth set -p app-api -w 'api-password'
255|spyro auth set -p app-ird -w 'ird-password'
256|spyro auth set -p app-recharge -w 'recharge-password'
257|```
258|
259|Use them independently:
260|
261|```bash
262|spyro artisan migrate:status -p app-api
263|spyro artisan tinker -p app-ird
264|spyro run "ls -la" -p app-recharge
265|```
266|
267|### Multiple servers
268|
269|```toml
270|[profiles.staging]
271|host = "10.0.0.1"
272|user = "deploy"
273|remote_path = "/var/www/app"
274|artisan = true
275|sudo = true
276|forwarded_ports = [3306]
277|
278|[profiles.production]
279|host = "10.0.0.2"
280|user = "deploy"
281|remote_path = "/var/www/app"
282|artisan = true
283|sudo = true
284|forwarded_ports = [3306]
285|```
286|
287|```bash
288|spyro artisan migrate -p staging     # staging only
289|spyro artisan migrate -p production  # production only
290|spyro artisan migrate --all          # both at once
291|```
292|
293|### WordPress profile
294|
295|```toml
296|[profiles.wordpress]
297|host = "wp.example.com"
298|user = "deploy"
299|remote_path = "/var/www/html"
300|wordpress = true
301|sudo = false
302|forwarded_ports = [33062]
303|
304|[profiles.wordpress.db]
305|host = "127.0.0.1"
306|port = 33062
307|name = "wordpress"
308|user = "wp_user"
309|password = ""
310|driver = "mysql"
311|```
312|
313|### Config file location
314|
315|Spyro walks up from your current directory looking for `spyro.toml`. Put it in your project root and run commands from anywhere inside the project tree.
316|
317|```
318|~/Projects/my-app/
319|├── spyro.toml          ← Spyro finds this
320|├── app/
321|│   ├── Http/
322|│   └── Models/
323|├── config/
324|└── routes/
325|```
326|
327|```bash
328|cd ~/Projects/my-app
329|spyro artisan migrate -p staging    # Works
330|
331|cd ~/Projects/my-app/app/Http
332|spyro artisan migrate -p staging    # Still works — walks up to find spyro.toml
333|```
334|
335|## Credentials
336|
337|### How it works
338|
339|Spyro stores **one password per profile** in your OS keychain (macOS Keychain / Linux Secret Service). This single password is used for both SSH login and sudo — because in practice, they're the same.
340|
341|If no keychain entry exists, Spyro prompts you interactively and stores it for next time.
342|
343|### Authentication commands
344|
345|```bash
346|# Store credentials (prompts securely)
347|spyro auth set -p staging
348|
349|# Store non-interactively (for scripts/CI)
350|spyro auth set -p staging -w 'my-password'
351|
352|# Overwrite existing without prompting
353|spyro auth set -p staging -w 'new-password' -f
354|
355|# List all stored credentials
356|spyro auth list
357|
358|# Delete a credential
359|spyro auth delete -p staging
360|```
361|
362|### What happens when you run a command
363|
364|```
365|spyro caddy restart -p dev
366|
367|1. Spyro checks keychain for dev credential
368|2. Found → uses it for SSH login
369|3. Command contains "sudo" → checks if profile has sudo=true
370|4. sudo=true → uses same credential for sudo prompt
371|5. sudo=false → prints clear error: "User 'deploy' does not have sudo access"
372|6. Command runs, credentials zeroed from memory immediately after
373|```
374|
375|### Security
376|
377|- Passwords never leave your OS keychain
378|- Never stored in `spyro.toml` or any config file
379|- Wrapped in `SecureCredential` (bytearray) during use, zeroed with triple-pass after
380|- All output sanitized against terminal injection attacks
381|- Shell arguments passed through `shlex.quote()` to prevent injection
382|
383|## Commands Reference
384|
385|### Tunnel Management
386|
387|```bash
388|spyro up                         # Start all tunnels (daemon mode)
389|spyro up staging                 # Start staging tunnel only
390|spyro up staging production      # Start multiple tunnels
391|spyro down                       # Stop all tunnels
392|spyro down staging               # Stop staging tunnel
393|spyro status                     # Show all active tunnels + health
394|spyro status staging             # Show staging tunnel details
395|```
396|
397|**Tunnel behavior:**
398|- Runs as daemon by default (survives terminal close)
399|- Self-healing: restarts on crash, network drop, or sleep/wake
400|- Exponential backoff: 1s → 2s → 4s → ... → 5min max
401|- Process tracking via `~/.spyro/tunnels.json` (orphan cleanup on reboot)
402|
403|### Laravel Artisan
404|
405|```bash
406|spyro artisan <command> -p <profile>
407|
408|# Examples:
409|spyro artisan migrate -p staging
410|spyro artisan migrate:status -p staging
411|spyro artisan queue:status -p staging
412|spyro artisan config:cache -p staging
413|spyro artisan route:cache -p staging
414|spyro artisan view:cache -p staging
415|spyro artisan optimize:clear -p staging
416|spyro artisan about -p staging
417|spyro artisan schedule:list -p staging
418|
419|# Run across all environments
420|spyro artisan queue:status --all
421|```
422|
423|**Tinker (interactive REPL):**
424|
425|```bash
426|spyro tinker -p staging                          # Interactive shell
427|spyro tinker -p staging -e "User::count()"       # One-shot eval
428|spyro tinker -p staging -f script.php            # Run a PHP file
429|```
430|
431|### Database
432|
433|```bash
434|# Tunnel + connection
435|spyro db tunnel -p staging              # Start tunnel, print connection URL
436|spyro db shell -p staging               # Open MySQL/MariaDB/psql prompt
437|spyro db ping -p staging                # Test connectivity
438|
439|# Querying
440|spyro db query "SELECT COUNT(*) FROM users" -p staging
441|spyro db query "SHOW TABLES" -p staging
442|
443|# Dumping
444|spyro db dump -p staging                           # Full dump
445|spyro db dump -p staging -t users,posts            # Specific tables
446|spyro db dump -p staging -t users -w "id > 100"   # With WHERE filter
447|spyro db dump -p staging -z                        # Gzip compressed
448|spyro db dump -p staging -d                        # Schema only (no data)
449|
450|# Listing
451|spyro db list-databases -p staging                 # List all databases
452|
453|# GUI tools
454|spyro proxy-url -p staging              # Generate connection string
455|spyro proxy-url -p staging | pbcopy     # Copy to clipboard
456|```
457|
458|**Proxy URL output:**
459|```
460|mysql://forge:@127.0.0.1:3306/myapp_staging
461|```
462|
463|Paste this into TablePlus, Sequel Ace, DBeaver, or any database GUI.
464|
465|### Service Management
466|
467|**Supervisor (queue workers, Reverb, etc.):**
468|
469|```bash
470|spyro supervisor status -p staging                    # All processes
471|spyro supervisor restart -p staging                   # Restart all
472|spyro supervisor restart laravel-queue -p staging     # Restart specific
473|spyro supervisor tail laravel-reverb -p staging       # Tail stderr logs
474|```
475|
476|**Redis:**
477|
478|```bash
479|spyro redis ping -p staging                    # Test connection
480|spyro redis stats -p staging                   # Connections, commands/s, keyspace
481|spyro redis info -p staging -s server          # Server info section
482|spyro redis info -p staging -s memory          # Memory info
483|spyro redis cli DBSIZE -p staging              # Run arbitrary redis-cli command
484|spyro redis cli KEYS "*" -p staging            # List all keys
485|```
486|
487|**PHP:**
488|
489|```bash
490|spyro php version -p staging                   # PHP version
491|spyro php extensions -p staging                # List loaded extensions
492|spyro php extensions -p staging --filter pdo   # Filter extensions
493|spyro php info -p staging --option memory_limit  # PHP config value
494|spyro php fpm-status -p staging                # PHP-FPM pool status
495|spyro php restart -p staging                   # Restart PHP-FPM
496|```
497|
498|**Web servers:**
499|
500|```bash
501|