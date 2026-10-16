"""scripts/release.py: version bump rules (incl. the minor cap of 99) and commit classification."""

from __future__ import annotations

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[2] / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_next_version_auto_and_explicit():
    assert release.next_version("1.2.3", "auto", {"fix"}) == "1.2.4"
    assert release.next_version("1.2.3", "auto", {"fix", "feat"}) == "1.3.0"
    assert release.next_version("1.2.3", "auto", {"feat", "breaking"}) == "2.0.0"
    assert release.next_version("1.2.3", "minor", set()) == "1.3.0"


def test_minor_cap_rolls_over_to_next_major():
    assert release.next_version("1.99.0", "minor", set()) == "2.0.0"
    assert release.next_version("1.99.4", "patch", set()) == "2.0.0"
    assert release.next_version("1.98.9", "minor", set()) == "1.99.0"


def test_classify():
    assert release.classify("feat(env): x", "") == "feat"
    assert release.classify("fix: x", "") == "fix"
    assert release.classify("feat!: x", "") == "breaking"
    assert release.classify("chore: x", "BREAKING CHANGE: y") == "breaking"
    assert release.classify("docs: x", "") == "other"


def test_notes_groups_sections():
    text = release.notes("1.3.0", [("abc1234", "feat: a", "feat"), ("def5678", "fix: b", "fix")])
    assert text.index("### Features") < text.index("### Fixes") and "- feat: a (abc1234)" in text
