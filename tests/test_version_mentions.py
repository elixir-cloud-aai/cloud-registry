"""Tests that the current release version is not hard-coded (drift guard).

The release version has one source (see "Versioning and releases" in
`docs/development.md`): the release tag and, on `dev`, the `version` of
`CITATION.cff`. The docs and the chart's README write it as `{{ version }}`
(replaced by `docs/hooks.py` and `scripts/chart_readme.py`); comments,
templates and tests use placeholders (e.g., `1.2.3`) and refer to sections by
anchor. So the current release version may only appear in the files and lines
of the allowlist below, which are about that release itself, not about the
latest one.
"""

import importlib.util
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("citation", ROOT / "scripts/citation.py")
assert _spec and _spec.loader
citation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(citation)

# Files that may name the release version: its source, and the release notes
ALLOWED_FILES = ("CITATION.cff",)
ALLOWED_DIRS = ("docs/releases/",)
# Lines that may name it (`{version}` is the escaped version): links to the
# release notes (also `mkdocs.yml` and the changelog), and migration headings
ALLOWED_LINES = (
    r"releases/{version}(?:\.md|/)",
    r"^#{{1,6}} (?:From versions before|To (?:app )?version) {version}$",
)


def mentions(text: str, version: str) -> list[tuple[int, str]]:
    """Return the lines that name a version, outside the allowed lines.

    Args:
        text: Content of a file.
        version: Release version, e.g., `1.2.3`.

    Returns:
        Number (from 1) and text of each line that names the version (also as
        `v1.2.3`, but not as part of another version, e.g., `11.2.3` or
        `1.2.30`), unless it matches one of `ALLOWED_LINES`.
    """
    escaped = re.escape(version)
    found = re.compile(rf"(?<![0-9.]){escaped}(?!\.?[0-9])")
    allowed = [re.compile(line.format(version=escaped)) for line in ALLOWED_LINES]
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if found.search(line) and not any(pattern.search(line) for pattern in allowed)
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("docker pull x:1.2.3", [(1, "docker pull x:1.2.3")]),
        ("a\n--source-ref refs/tags/v1.2.3", [(2, "--source-ref refs/tags/v1.2.3")]),
        ("Release 1.2.3.", [(1, "Release 1.2.3.")]),
        ("x-1.2.3rc1.tgz", [(1, "x-1.2.3rc1.tgz")]),
        # Other versions
        ("11.2.3 1.2.30 1.2.3.4 0.1.2.3", []),
        ("{{ version }}", []),
        # Allowed lines
        ("- [1.2.3](releases/1.2.3.md)", []),
        ('      - "1.2.3": releases/1.2.3.md', []),
        ("See [notes](https://x.example.org/en/latest/releases/1.2.3/).", []),
        ("## From versions before 1.2.3", []),
        ("### To app version 1.2.3", []),
        ("Text about versions before 1.2.3", [(1, "Text about versions before 1.2.3")]),
    ],
)
def test_mentions(text: str, expected: list[tuple[int, str]]) -> None:
    """Lines naming the version are found, unless they are allowed.

    Args:
        text: Content of a file.
        expected: Expected lines.
    """
    assert mentions(text, "1.2.3") == expected


def _tracked_files() -> list[str]:
    """Return the files tracked by Git, relative to the repository root.

    Returns:
        The paths, as `git ls-files` lists them.
    """
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("needs a Git checkout")
    listed = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [git, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    return [name for name in listed.stdout.decode().split("\0") if name]


def test_release_version_not_hard_coded() -> None:
    """The current release version appears only where the allowlist allows."""
    version = citation.release_version(
        (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    )
    if version is None:
        pytest.skip("CITATION.cff has no version (no release yet)")
    found = []
    for name in _tracked_files():
        if name in ALLOWED_FILES or name.startswith(ALLOWED_DIRS):
            continue
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += [
            f"{name}:{number}: {line}" for number, line in mentions(text, version)
        ]
    assert found == [], (
        f"The release version {version} is hard-coded; use {{{{ version }}}} in"
        " the docs and the chart's README, placeholders (e.g., 1.2.3) elsewhere,"
        " or extend the allowlist of this test if the line is about that"
        " release itself:\n" + "\n".join(found)
    )
