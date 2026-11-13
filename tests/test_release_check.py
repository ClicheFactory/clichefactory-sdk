"""scripts/release_check.py: the gate the release workflow runs before building."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_check.py"
_spec = importlib.util.spec_from_file_location("release_check", _SCRIPT)
release_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_check)

CHANGELOG = """# Changelog

## [0.9.0] — 2026-11-20

### Added

- Balance.

## [0.9.0rc1] — 2026-11-15

### Added

- Balance (release candidate).

## [0.8.0] — 2026-07-22

- Older.
"""


@pytest.fixture
def repo(tmp_path: Path):
    def make(version: str) -> tuple[Path, Path]:
        about = tmp_path / "__about__.py"
        about.write_text(f'__version__ = "{version}"\n')
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(CHANGELOG)
        return about, changelog

    return make


def test_matching_tag_returns_its_changelog_entry(repo):
    about, changelog = repo("0.9.0")
    version, notes = release_check.check("v0.9.0", about, changelog)
    assert version == "0.9.0"
    assert notes == "### Added\n\n- Balance."


def test_release_candidate_has_its_own_entry(repo):
    about, changelog = repo("0.9.0rc1")
    version, notes = release_check.check("v0.9.0rc1", about, changelog)
    assert notes == "### Added\n\n- Balance (release candidate)."
    assert release_check.is_prerelease(version)


def test_last_entry_in_the_file(repo):
    about, changelog = repo("0.8.0")
    assert release_check.check("v0.8.0", about, changelog)[1] == "- Older."


@pytest.mark.parametrize(
    "tag, version, reason",
    [
        ("v0.9.1", "0.9.0", "does not match"),
        ("0.9.0", "0.9.0", "must look like"),
        ("v1.0.0", "1.0.0", "no '## [1.0.0]"),
    ],
)
def test_mismatches_fail_with_the_reason(repo, tag, version, reason):
    about, changelog = repo(version)
    with pytest.raises(release_check.ReleaseError, match=reason.replace("[", r"\[")):
        release_check.check(tag, about, changelog)


@pytest.mark.parametrize(
    "version, pre",
    [("0.9.0", False), ("1.0.0", False), ("0.9.0rc1", True), ("1.0.0a2", True),
     ("1.0.0b1", True), ("1.0.0.dev3", True)],
)
def test_prerelease_detection(version, pre):
    assert release_check.is_prerelease(version) is pre


def test_current_repo_version_has_a_changelog_entry():
    """The version on main must always be releasable as-is."""
    version = release_check.package_version()
    assert release_check.changelog_entry(version) is not None
