"""Agent Skills for spyro: one ``SKILL.md`` per command, generated from the Click tree.

The option tables come straight from ``--help`` so they cannot drift from the
code; ``NOTES`` holds the hand-written gotchas an agent cannot infer from help
text. ``spyro install ai-skills`` writes the result where agents look for skills
(agentskills.io layout: ``<dir>/<skill-name>/SKILL.md``).
"""

from __future__ import annotations

from pathlib import Path

import click

# Per-command gotchas. Keep each to the facts an agent needs before running it.
NOTES: dict[str, str] = {
    "apache": "Service group for Apache hosts (`status`, `version`, `modules`, `sites`, `restart`). `restart` needs a profile with `sudo = true`.",
    "artisan": (
        "Runs `php artisan <args>` inside `remote_path` (its Capistrano `current/` when present). "
        "On a `sudo = true` profile it runs as `sudo_user` (or root); `--no-escalate` runs as the SSH user. "
        "Needs `artisan = true` on the profile. Pass artisan flags after the command: "
        "`spyro artisan migrate --force -p staging`. The remote exit status is spyro's exit status."
    ),
    "auth": (
        "Passwords live only in the OS keychain (service `spyro-cli`), never in spyro.toml. "
        "Non-interactive runs use `SPYRO_PASSWORD` or `SPYRO_PASSWORD_<PROFILE>` (upper-case) instead."
    ),
    "caddy": "Service group for Caddy hosts (`status`, `version`, `restart`). `restart` needs a profile with `sudo = true`.",
    "config": "`config validate` checks spyro.toml offline. To see what a profile resolves to, use `spyro profiles`.",
    "cp": (
        "Remote side: `:relative` resolves against the profile's `remote_path`; `/abs` and `~/x` are used as given "
        "(no colon). `--home` resolves relative paths against the SSH home instead. "
        "Use `-r` for directories, `--parents` to create missing remote dirs. "
        "Prefer `-p <profile>` over `--all`; `--all` also hits production profiles."
    ),
    "db": (
        "Every subcommand needs `forwarded_ports` on the profile containing `db.port`; without it they fail with "
        "'no forwarded_ports configured'. `db tunnel` STARTS a tunnel (side effect) and prints the URL. "
        "An empty `db.password` makes spyro read the remote `.env`. Passwords reach clients via "
        "`MYSQL_PWD`/`PGPASSWORD`, never on a command line."
    ),
    "doctor": (
        "Has NO `-p` filter: it connects to EVERY profile in spyro.toml, including production ones. "
        "Do not run it when any profile must not be touched; use `spyro profiles` and per-profile commands instead."
    ),
    "down": "Stops the tunnel for one profile; with no argument stops ALL profiles' tunnels. Always pass the profile.",
    "env": (
        "`env diff` never prints values (every value is `***`), only which keys differ. "
        "`env pull` writes the remote .env to a local file: never cat or paste its contents into a chat. "
        "`env push` overwrites the remote .env; the local file must exist."
    ),
    "eval": (
        "PHP only. Boots Laravel (`bootstrap/app.php`) and evaluates one expression; needs `artisan = true`. "
        "Use `--json` for machine-readable stdout (progress goes to stderr). On a `sudo = true` profile it runs "
        "as `sudo_user`; `--no-escalate` runs as the SSH user. For shell, Python, Node or Docker use `spyro run`. "
        "Prefer `eval` over `tinker -e`: no PsySH quoting problems."
    ),
    "init": "Creates a spyro.toml in the current directory and checks ssh/scp are installed. Refuses if a spyro.toml already exists up the tree. Does not contact servers.",
    "install": "`install ai-skills` writes these SKILL.md files into every detected agent skills directory.",
    "logs": (
        "`logs laravel` tails the NEWEST `storage/logs/laravel*.log` (daily files included). "
        "`--level X` keeps entries at X or worse with their stack-trace lines; `-g` is an awk extended regex "
        "(no GNU `\\b`/`\\d`) applied line by line; with both, grep runs after level, so non-matching stack lines "
        "drop out. `-n` caps matches when filtering; filtered runs scan the whole newest file. "
        "`-f` streams with no timeout. Other subcommands tail nginx/apache/php logs; `logs supervisor <profile>` "
        "is spyro's own local tunnel log."
    ),
    "nginx": "`nginx status` runs `nginx -t` (sudo on `sudo = true` profiles); `restart`/`reload` need sudo.",
    "php": "`php version/extensions/fpm-status/info/restart`. `info -o memory_limit` greps `php -i`; for `php --ini` use `spyro run`.",
    "pin": "Registers a local directory for `spyro sync`; `-f` picks the exclusion rules (laravel, wordpress, node, python).",
    "pins": "Lists pinned directories. Read-only.",
    "profiles": (
        "Local only, no network, never prints passwords. Run this FIRST to learn each profile's host, user, "
        "`remote_path` (`(default)` means spyro.toml did not set it), sudo and forwarded ports. "
        "Never guess remote paths. `--json` for scripting."
    ),
    "ps": "Uses a plain `ssh` (no keychain password injection): needs key/agent auth or a live ControlMaster.",
    "redis": "`redis-cli` wrappers: `ping`, `info [-s section]`, `stats`, and `cli <args>` for anything else (that one can write; be careful with FLUSHALL etc.).",
    "run": (
        "Runs the command string as-is as the SSH user. Starts in the SSH HOME; pass `-C` to start in "
        "`remote_path` (Capistrano `current/` aware): without `-C`, relative paths and `php artisan` miss the app. "
        "spyro never adds `sudo`; write it yourself, and only on a `sudo = true` profile (that flag only "
        "allocates a tty and answers the sudo password prompt). The remote exit status is spyro's; `--all` runs "
        "every profile (including production) and exits with the first failure, so prefer `-p`. "
        "Default timeout 60s (`--timeout`). Anything installed remotely works: php, python3, docker, node."
    ),
    "script": (
        "Uploads a local .php file and runs it with plain `php` from `remote_path`; it does not boot Laravel, "
        "so the file must `require 'vendor/autoload.php'` itself. Temp file is always removed. "
        "Escalates like `eval` on `sudo = true` profiles. Not for Python/Node: use `cp` + `run`."
    ),
    "ssh": "Interactive shell starting in `remote_path` (when set); `--home` starts in HOME. Needs a real terminal.",
    "status": "Local tunnel state from `~/.spyro/tunnels.json`; `stale` means the ssh process died. `--json` available.",
    "supervisor": (
        "`status/start/stop/restart/tail` for supervisord programs (e.g. `horizon`). "
        "Needs a `sudo = true` profile; a plain app user gets exit 1 ('does not have sudo access')."
    ),
    "sync": "Uploads pinned directories (`spyro pin`) when files change; `--dry-run` first. Secrets/caches are excluded by framework rules.",
    "tinker": (
        "`php artisan tinker`: interactive (needs a tty), `-e` one expression via PsySH, `-f` feeds a file. "
        "For scripted one-shots prefer `spyro eval` (reliable quoting, `--json`)."
    ),
    "unpin": "Removes a pinned directory from sync. Local only.",
    "up": "Starts tunnels for the given profile (or EVERY profile with `forwarded_ports` when omitted). Always pass the profile.",
    "update": "Self-update from GitHub tags; `--check` only reports. Needs `git` and `uv` on PATH.",
    "wp": "WP-CLI wrapper for `wordpress = true` profiles; runs in `remote_path` with the same sudo rules as `artisan`.",
}

OVERVIEW_RULES = """\
## Rules every agent must follow

1. **Read before acting.** Run `spyro profiles` (local, safe) to learn each profile's host, user and
   `remote_path`. Never guess remote paths; never assume `-C`/`remote_path` when `profiles` shows `(default)`.
2. **Name the profile on remote commands** with `-p <name>` (`up`/`down`/`logs supervisor` take it as an
   argument; `artisan`/`ssh`/`ps` fall back to `[defaults].profile` or the sole profile). Local commands
   (`profiles`, `status`, `init`, `install`, `update`, `pins`, `config`, `auth`) need no profile; `profiles -p`
   is an optional output filter. Avoid `--all`,
   bare `up`/`down`, and `doctor`: they touch every profile in `spyro.toml`, including production. Never run
   anything against a production profile unless the user explicitly asked for that profile in this conversation.
3. **Secrets stay on the server.** Never print `.env` contents, `env pull` output, passwords or tokens.
   `env diff` is masked for this reason. Passwords come from the OS keychain or `SPYRO_PASSWORD[_<PROFILE>]`.
4. **Pick the right executor:** shell/Python/Docker → `spyro run` (`-C` for the app dir); Laravel expression
   → `spyro eval`; Artisan → `spyro artisan`; local PHP file → `spyro script`; interactive → `spyro ssh`/`tinker`.
5. **Exit codes are real.** A failing remote command fails spyro; check it instead of parsing output.
6. **`sudo` is never added for you** by `run`; app commands (`artisan`, `eval`, `script`, `wp`, `tinker`)
   escalate only on `sudo = true` profiles (`--no-escalate` disables).
"""


def _help(cmd: click.Command, path: str) -> str:
    ctx = click.Context(cmd, info_name=path, terminal_width=100)
    return cmd.get_help(ctx)


def _sections(cmd: click.Command, path: str) -> list[str]:
    out = [f"### `{path}`\n\n```text\n{_help(cmd, path)}\n```"]
    if isinstance(cmd, click.Group):
        for sub_name in sorted(cmd.commands):
            out.extend(_sections(cmd.commands[sub_name], f"{path} {sub_name}"))
    return out


MARKER = "<!-- generated by `spyro install ai-skills`; edits are overwritten on reinstall -->"


def _first_sentence(cmd: click.Command) -> str:
    return (cmd.get_short_help_str(limit=200) or cmd.name or "").rstrip(".")


def _command_skill(name: str, cmd: click.Command) -> str:
    desc = (
        f"Use when running `spyro {name}` ({_first_sentence(cmd)}) against a remote server profile; "
        f"exact flags, subcommands and gotchas so no option is guessed."
    )
    body = "\n\n".join(_sections(cmd, f"spyro {name}"))
    return (
        f"---\nname: spyro-{name}\ndescription: {desc}\n---\n\n{MARKER}\n\n"
        f"# spyro {name}\n\n{_first_sentence(cmd)}.\n\n"
        f"## Before you run it\n\n{NOTES[name]}\n\n"
        f"Read `spyro profiles` first for hosts and `remote_path`; the `spyro` skill has the safety rules.\n\n"
        f"## Reference (from `--help`)\n\n{body}\n"
    )


def _overview(cli: click.Group) -> str:
    rows = "\n".join(
        f"| `spyro {n}` | {_first_sentence(c)} | `spyro-{n}` |" for n, c in sorted(cli.commands.items())
    )
    desc = (
        "Use for any task on a remote server managed with the spyro CLI (SSH tunnels, remote commands, "
        "Laravel artisan/eval, logs, .env, DB access, file copy): how to pick the command, name the profile, "
        "and avoid touching production or leaking secrets."
    )
    return (
        f"---\nname: spyro\ndescription: {desc}\n---\n\n{MARKER}\n\n"
        "# spyro\n\nSSH tunneling and remote-command CLI driven by a project `spyro.toml` of profiles "
        "(`staging`, `production`, ...). Remote commands select a profile with `-p <profile>`; "
        "local ones (`profiles`, `status`, `install`, ...) need none.\n\n"
        f"{OVERVIEW_RULES}\n## Commands\n\n| Command | Purpose | Skill |\n|---|---|---|\n{rows}\n\n"
        "Each command has its own skill with the full `--help` output and gotchas.\n"
    )


def generate(cli: click.Group | None = None) -> dict[str, str]:
    """Return ``{skill_name: SKILL.md text}`` for the overview and every top-level command."""
    if cli is None:
        from .cli.main import main as cli
    assert isinstance(cli, click.Group)
    out = {"spyro": _overview(cli)}
    for name, cmd in sorted(cli.commands.items()):
        out[f"spyro-{name}"] = _command_skill(name, cmd)
    return out


# Where agents look for skills: (dir that proves the agent is installed, skills dir under it).
AGENT_DIRS: tuple[tuple[str, str], ...] = (
    (".agents", ".agents/skills"),  # cross-client (agentskills.io)
    (".claude", ".claude/skills"),
    (".codex", ".codex/skills"),
    (".cursor", ".cursor/skills"),
    (".omp/agent", ".omp/agent/skills"),
    (".gemini", ".gemini/skills"),
)
FALLBACK_DIR = ".agents/skills"


def detect_targets(root: Path) -> list[Path]:
    """Skills dirs under *root* for every agent that is installed there; the cross-client dir if none."""
    found = [root / skills for marker, skills in AGENT_DIRS if (root / marker).is_dir()]
    return found or [root / FALLBACK_DIR]


def install(
    targets: list[Path], generated: dict[str, str] | None = None, *, force: bool = False
) -> tuple[list[Path], list[Path]]:
    """Write every skill to each target as ``<target>/<name>/SKILL.md``.

    Files spyro generated earlier (they carry ``MARKER``) are overwritten; anything
    else at that path is left alone unless *force*. Returns ``(written, kept)``.
    """
    generated = generated or generate()
    written: list[Path] = []
    kept: list[Path] = []
    for target in targets:
        for name, text in generated.items():
            path = target / name / "SKILL.md"
            if path.exists() and not force and MARKER not in path.read_text(encoding="utf-8", errors="replace"):
                kept.append(path)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
    return written, kept


def pick_by_name(generated: dict[str, str], names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Subset of *generated* for *names*; ``run`` and ``spyro-run`` both select ``spyro-run``.

    Raises ``KeyError(name)`` for an unknown name.
    """
    out: dict[str, str] = {}
    for raw in names:
        key = raw if raw in generated else f"spyro-{raw}"
        if key not in generated:
            raise KeyError(raw)
        out[key] = generated[key]
    return out


def parse_selection(answer: str, count: int) -> list[int]:
    """``"1 3-5"`` → ``[1, 3, 4, 5]``; ``"all"`` → every index; empty → ``[]``. 1-based, deduplicated.

    Raises ``ValueError`` for anything that is not a number/range within ``1..count``.
    """
    answer = answer.strip()
    if not answer:
        return []
    if answer.lower() == "all":
        return list(range(1, count + 1))
    chosen: list[int] = []
    for token in answer.replace(",", " ").split():
        lo, dash, hi = token.partition("-")
        if not lo.isdigit() or (dash and not hi.isdigit()):
            raise ValueError(f"not a number or range: {token!r}")
        start, end = int(lo), int(hi) if hi else int(lo)
        if not (1 <= start <= end <= count):
            raise ValueError(f"{token!r} is outside 1-{count} or reversed")
        chosen.extend(i for i in range(start, end + 1) if i not in chosen)
    return chosen
