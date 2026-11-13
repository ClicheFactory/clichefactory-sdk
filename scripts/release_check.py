"""Check a release tag against the package version and the changelog.

Usage:

    python scripts/release_check.py v0.9.0
    python scripts/release_check.py v0.9.0rc1 --notes-out notes.md

Fails (exit 1, with the reason) unless:

* the tag is ``v`` + the version in ``clichefactory/__about__.py``, and
* ``CHANGELOG.md`` has a ``## [<version>]`` entry.

``--notes-out`` writes that entry, which becomes the GitHub release notes.
In GitHub Actions it also sets the step outputs ``version`` and
``prerelease`` (true for ``aN``, ``bN``, ``rcN`` and ``.devN`` versions).

Uses only the standard library, so it runs before anything is installed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ABOUT = ROOT / "clichefactory" / "__about__.py"
CHANGELOG = ROOT / "CHANGELOG.md"

_PRERELEASE = re.compile(r"(a|b|rc|\.dev)\d+$")


class ReleaseError(Exception):
    pass


def package_version(about: Path = ABOUT) -> str:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', about.read_text(), re.M)
    if not m:
        raise ReleaseError(f"no __version__ in {about.relative_to(ROOT)}")
    return m.group(1)


def changelog_entry(version: str, changelog: Path = CHANGELOG) -> str | None:
    """The body of ``## [version] — date``, up to the next ``## [`` heading."""
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S
    )
    m = pattern.search(changelog.read_text())
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def is_prerelease(version: str) -> bool:
    return bool(_PRERELEASE.search(version))


def check(tag: str, about: Path = ABOUT, changelog: Path = CHANGELOG) -> tuple[str, str]:
    """Return (version, release notes), or raise ReleaseError saying what's wrong."""
    if not tag.startswith("v"):
        raise ReleaseError(f"tag {tag!r} must look like v1.2.3 or v1.2.3rc1")
    version = tag[1:]
    pkg = package_version(about)
    if version != pkg:
        raise ReleaseError(
            f"tag {tag} does not match clichefactory/__about__.py ({pkg}). "
            f"Bump __about__.py, or tag v{pkg}."
        )
    notes = changelog_entry(version, changelog)
    if notes is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}] — YYYY-MM-DD' entry")
    return version, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tag", help="the git tag, e.g. v0.9.0")
    parser.add_argument("--notes-out", type=Path, help="write the changelog entry here")
    args = parser.parse_args(argv)

    try:
        version, notes = check(args.tag)
    except ReleaseError as exc:
        print(f"release check failed: {exc}", file=sys.stderr)
        return 1

    if args.notes_out:
        args.notes_out.write_text(notes + "\n")
    prerelease = is_prerelease(version)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"version={version}\nprerelease={str(prerelease).lower()}\n")
    kind = "pre-release" if prerelease else "release"
    print(f"{args.tag}: {kind} {version}, changelog entry found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
