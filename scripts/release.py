#!/usr/bin/env python3
"""Bump the version files and draft release notes from commits since the last tag.

usage: release.py [auto|patch|minor|major] [--notes FILE]   (prints the new version)

auto reads Conventional Commits: `!:` / BREAKING CHANGE -> major, feat -> minor, else patch.
Spyro rule (AGENTS.md): minor never exceeds 99, so from X.99 any bump is the next major.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {ROOT / "pyproject.toml": r'(?m)^(version = ")[^"]+(")', ROOT / "src/spyro/__init__.py": r'(?m)^(__version__ = ")[^"]+(")'}
SECTIONS = [("breaking", "Breaking changes"), ("feat", "Features"), ("fix", "Fixes"), ("other", "Other")]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def classify(subject: str, body: str) -> str:
    m = re.match(r"(\w+)(\([^)]*\))?(!)?:", subject)
    if (m and m.group(3)) or "BREAKING CHANGE" in body:
        return "breaking"
    return m.group(1) if m and m.group(1) in ("feat", "fix") else "other"


def commits_since(tag: str) -> list[tuple[str, str, str]]:
    """(short hash, subject, kind) for every commit after *tag*, release commits skipped."""
    out = git("log", f"{tag}..HEAD", "--format=%h%x00%s%x00%b%x01") if tag else ""
    rows = []
    for rec in filter(None, (r.strip() for r in out.split("\x01"))):
        h, subject, body = rec.split("\x00", 2)
        if not re.match(r"v\d+\.\d+\.\d+:", subject):  # earlier release commits
            rows.append((h, subject, classify(subject, body)))
    return rows


def next_version(cur: str, bump: str, kinds: set[str]) -> str:
    major, minor, patch = map(int, cur.split("."))
    if bump == "auto":
        bump = "major" if "breaking" in kinds else "minor" if "feat" in kinds else "patch"
    if minor >= 99 and bump != "major":  # ponytail: cap rolls over by rule; no X.99.y patch releases
        bump = "major"
    return {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}[bump]


def notes(version: str, rows: list[tuple[str, str, str]]) -> str:
    lines = [f"## v{version}", ""]
    for kind, title in SECTIONS:
        items = [f"- {s} ({h})" for h, s, k in rows if k == kind]
        if items:
            lines += [f"### {title}", *items, ""]
    if len(lines) == 2:
        lines += ["No user-facing changes.", ""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> None:
    bump = next((a for a in argv if a in ("auto", "patch", "minor", "major")), "auto")
    notes_file = argv[argv.index("--notes") + 1] if "--notes" in argv else None
    current = re.search(r'(?m)^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text()).group(1)
    try:
        last_tag = git("describe", "--tags", "--abbrev=0", "--match", "v*")
    except subprocess.CalledProcessError:
        last_tag = ""
    rows = commits_since(last_tag)
    version = next_version(current, bump, {k for _, _, k in rows})
    for path, pattern in FILES.items():
        path.write_text(re.sub(pattern, rf"\g<1>{version}\g<2>", path.read_text(), count=1))
    if notes_file:
        Path(notes_file).write_text(notes(version, rows))
    print(version)


if __name__ == "__main__":
    main(sys.argv[1:])
