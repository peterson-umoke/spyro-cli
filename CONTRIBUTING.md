# Contributing

Spyro is a developer tool — patches that make it faster, more reliable, or easier to use are welcome.

## Getting Started

```bash
git clone https://github.com/peterson-umoke/spyro-cli.git
cd spyro-cli
uv sync
uv run pytest
```

## Before You Code

Open an issue first for anything beyond a small bug fix. Saves you writing code that won't merge.

## Standards

- **One concern per function.** If you can't name what it does in under 10 words, split it.
- **500-line file limit.** Reached it? Probably time to extract a module.
- **Tests or it didn't happen.** Bug fixes get a regression test. New features get coverage.
- **No drive-by refactors.** Changing style or restructuring belongs in its own PR.

## Pull Requests

1. Branch from `main`
2. Keep PRs focused — one feature or fix per PR
3. Run `pytest` and `pip-audit` before opening
4. Write a changelog entry in the PR description

## Security

This tool handles SSH credentials. If you find a security issue, don't open a public issue — see SECURITY.md.
