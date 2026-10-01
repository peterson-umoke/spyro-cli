"""``spyro install ai-skills``: one SKILL.md per command, generated from the live Click tree.

Agents read these instead of guessing flags, so every option and subcommand that
``--help`` shows must appear in the skill, and the files must install where
agents look (``~/.claude/skills``, ``~/.agents/skills``, ...).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from spyro import skills
from spyro.cli.main import main

REPO_SKILLS = Path(__file__).resolve().parents[2] / "skills"


def invoke(*args):
    return CliRunner().invoke(main, list(args))


def frontmatter(text: str) -> dict[str, str]:
    head, _, _ = text.partition("\n---\n")
    assert head.startswith("---\n"), text[:80]
    return dict(line.split(": ", 1) for line in head.splitlines()[1:] if line)


class TestGeneration:
    @pytest.fixture(scope="class")
    def generated(self):
        return skills.generate()

    def test_one_skill_per_top_level_command_plus_overview(self, generated):
        expected = {f"spyro-{name}" for name in main.commands} | {"spyro"}
        assert set(generated) == expected

    def test_frontmatter_is_valid_and_name_matches_directory(self, generated):
        for name, text in generated.items():
            fm = frontmatter(text)
            assert fm["name"] == name
            assert 0 < len(fm["description"]) <= 1024, name
            assert "\n" not in fm["description"]

    def test_options_and_subcommands_from_help_are_present(self, generated):
        run = generated["spyro-run"]
        for flag in ("--all", "-p, --profile", "--timeout", "-C, --chdir"):
            assert flag in run, flag
        logs = generated["spyro-logs"]
        for text in ("spyro logs laravel", "--level", "-g, --grep", "spyro logs nginx-error"):
            assert text in logs, text
        db = generated["spyro-db"]
        assert "spyro db tunnel" in db and "spyro db dump" in db

    def test_curated_notes_are_included(self, generated):
        assert "-C" in generated["spyro-run"] and "remote_path" in generated["spyro-run"]
        assert "PHP" in generated["spyro-eval"] and "spyro run" in generated["spyro-eval"]
        assert "does not boot Laravel" in generated["spyro-script"]

    def test_overview_lists_every_command_and_the_safety_rules(self, generated):
        overview = generated["spyro"]
        for name in main.commands:
            assert f"spyro-{name}" in overview, name
        assert "spyro profiles" in overview
        assert "production" in overview

    def test_every_command_has_a_note(self):
        assert set(skills.NOTES) == set(main.commands)


class TestInstall:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        return tmp_path

    def test_installs_into_every_agent_dir_that_exists(self, home):
        (home / ".claude").mkdir()
        (home / ".omp" / "agent").mkdir(parents=True)
        result = invoke("install", "ai-skills")
        assert result.exit_code == 0, result.output
        for root in (".claude/skills", ".omp/agent/skills"):
            assert (home / root / "spyro-run" / "SKILL.md").is_file(), root
            assert (home / root / "spyro" / "SKILL.md").is_file(), root
        assert not (home / ".codex").exists()
        assert ".claude/skills" in result.output and ".omp/agent/skills" in result.output

    def test_falls_back_to_the_cross_agent_dir_when_no_agent_is_installed(self, home):
        result = invoke("install", "ai-skills")
        assert result.exit_code == 0, result.output
        assert (home / ".agents" / "skills" / "spyro-run" / "SKILL.md").is_file()

    def test_dest_overrides_detection_and_overwrites_only_spyro_generated_files(self, home):
        dest = home / "custom"
        ours = dest / "spyro-run" / "SKILL.md"
        theirs = dest / "spyro" / "SKILL.md"
        ours.parent.mkdir(parents=True)
        theirs.parent.mkdir(parents=True)
        ours.write_text(f"stale\n{skills.MARKER}\n")
        theirs.write_text("hand-written\n")
        (home / ".claude").mkdir()
        result = invoke("install", "ai-skills", "--dest", str(dest))
        assert result.exit_code == 0, result.output
        assert "name: spyro-run" in ours.read_text()
        assert theirs.read_text() == "hand-written\n" and "kept" in result.output
        assert not (home / ".claude" / "skills").exists()
        invoke("install", "ai-skills", "--dest", str(dest), "--force")
        assert "name: spyro\n" in theirs.read_text()

    def test_dry_run_writes_nothing(self, home):
        (home / ".claude").mkdir()
        result = invoke("install", "ai-skills", "--dry-run")
        assert result.exit_code == 0 and ".claude/skills" in result.output
        assert not (home / ".claude" / "skills").exists()

    def test_project_scope_uses_the_current_directory(self, home, monkeypatch):
        proj = home / "proj"
        (proj / ".claude").mkdir(parents=True)
        (home / ".claude").mkdir()
        monkeypatch.chdir(proj)
        result = invoke("install", "ai-skills", "--project")
        assert result.exit_code == 0, result.output
        assert (proj / ".claude" / "skills" / "spyro" / "SKILL.md").is_file()
        assert not (home / ".claude" / "skills").exists()

    def test_named_skills_install_only_those(self, home):
        dest = home / "d"
        result = invoke("install", "ai-skills", "run", "spyro-logs", "--dest", str(dest))
        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in dest.iterdir()) == ["spyro-logs", "spyro-run"]

    def test_unknown_skill_name_is_rejected_with_the_available_names(self, home):
        result = invoke("install", "ai-skills", "nope", "--dest", str(home / "d"))
        assert result.exit_code == 2 and "spyro-run" in result.output

    def test_select_installs_the_picked_numbers_and_ranges(self, home):
        dest = home / "d"
        names = sorted(skills.generate())
        result = CliRunner().invoke(
            main, ["install", "ai-skills", "--select", "--dest", str(dest)], input="1 3-4\n"
        )
        assert result.exit_code == 0, result.output
        assert sorted(p.name for p in dest.iterdir()) == sorted([names[0], names[2], names[3]])
        for i, name in enumerate(names, 1):
            assert f"{i:2}) {name}" in result.output

    def test_select_accepts_all_and_an_empty_answer_installs_nothing(self, home):
        dest = home / "d"
        assert CliRunner().invoke(main, ["install", "ai-skills", "--select", "--dest", str(dest)], input="all\n").exit_code == 0
        assert len(list(dest.iterdir())) == len(skills.generate())
        other = home / "e"
        result = CliRunner().invoke(main, ["install", "ai-skills", "--select", "--dest", str(other)], input="\n")
        assert result.exit_code == 0 and not other.exists() and "Nothing selected" in result.output

    def test_select_rejects_out_of_range_numbers(self, home):
        result = CliRunner().invoke(main, ["install", "ai-skills", "--select", "--dest", str(home / "d")], input="99\n")
        assert result.exit_code == 2


def test_checked_in_skills_match_the_generator():
    """``skills/`` is what GitHub and ``gh skill install`` see; regenerate with
    ``spyro install ai-skills --dest skills`` when a command or note changes."""
    generated = skills.generate()
    on_disk = {p.parent.name: p.read_text() for p in REPO_SKILLS.glob("*/SKILL.md")}
    assert on_disk == generated
