"""Check curated release notes and build the body of a GitHub release.

Release notes follow the format agreed for all repositories of the
organization (see "Versioning and releases" in `docs/development.md`):

1. An intro paragraph saying what the release brings, with a link to the
   upgrade or migration guide if there is one.
2. Grouped sections, in the order of `SECTIONS`, with empty ones left out;
   each bullet ends with its issue and pull request numbers, e.g.,
   `(#12, #34)`.
3. `## Commits`: GitHub's generated notes, verbatim (including "New
   Contributors"), without their `## What's Changed` header.
4. Last, GitHub's generated `**Full Changelog**: ...` line.

The curated notes (`docs/releases/<version>.md`) hold parts 1 and 2; the
release workflow (`.github/workflows/release.yaml`) adds parts 3 and 4 from
GitHub's `generate-notes` API.

Commands:
    `check <notes>`: check the structure of curated notes.
    `body <notes> <generated>`: check curated notes, then print the body of
        the release to standard output: the curated notes, then the
        generated notes (the `body` of the `generate-notes` response, in a
        file).

Errors are printed as GitHub annotations to standard error. Only the stdlib
is used, so the workflow runs it without installing the app.
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import sys

# Headings of the curated notes, in their order; all are optional
SECTIONS = (
    "Breaking changes",
    "Features",
    "Security",
    "Fixes",
    "Documentation",
    "Maintenance",
)
# Added by the release workflow, after the curated sections
COMMITS = "Commits"
# First line of GitHub's generated notes, which is dropped
WHATS_CHANGED = "## What's Changed"
# Start of the last line of GitHub's generated notes, which the curated notes
# must not have
FULL_CHANGELOG = "**Full Changelog**"

# ATX headings (`## Title`)
HEADING = re.compile(
    r" {0,3}(?P<level>#{1,6})(?:[ \t]+(?P<title>.*?))??(?:[ \t]+#*)?[ \t]*"
)
# Setext heading underlines (`---`, `===`) and thematic breaks (also `***`,
# `___`), which are rejected: a line of `-` or `=` under a paragraph makes it
# a heading
UNDERLINE = re.compile(r" {0,3}(?:(?:-[ \t]*)+|=+[ \t]*|(?:[*_][ \t]*){3,})")
# Opening and closing lines of fenced code blocks
FENCE = re.compile(r"[ \t]*(?:```|~~~)")
# Top-level list items (bullets or numbered)
ITEM = re.compile(r"(?:[-*+]|[0-9]+[.)])(?:[ \t]|$)")
# The end of a bullet: its issue and pull request numbers, e.g., `(#12, #34)`
REFERENCES = re.compile(r".*\(#[0-9]+(?:[,;][ \t]*#[0-9]+)*\)", re.ASCII)


@dataclass(frozen=True)
class Finding:
    """A problem in the structure of curated release notes.

    Attributes:
        line: Number of the line (from 1).
        message: What is wrong.
    """

    line: int
    message: str


def _heading_finding(
    number: int, level: int, title: str, last: int | None
) -> Finding | None:
    """Return the problem of a heading of curated notes, if any.

    Args:
        number: Number of the heading's line.
        level: Level of the heading (number of `#`).
        title: Title of the heading.
        last: Index in `SECTIONS` of the last valid section before, if any.

    Returns:
        A finding unless the heading is a section of `SECTIONS` after `last`.
    """
    allowed = ", ".join(SECTIONS)
    if level != 2:
        return Finding(
            number,
            f"Heading of level {level} ('{title}'): only sections"
            f" ('## <title>') are allowed: {allowed}",
        )
    if title == COMMITS:
        return Finding(
            number,
            f"Section '{COMMITS}': the release workflow adds it from GitHub's"
            " generated notes",
        )
    if title not in SECTIONS:
        return Finding(
            number, f"Unknown section '{title}': allowed are, in this order: {allowed}"
        )
    if last is not None and SECTIONS.index(title) <= last:
        return Finding(
            number,
            f"Section '{title}' after '{SECTIONS[last]}': sections are in this"
            f" order, each once: {allowed}",
        )
    return None


def check(markdown: str) -> list[Finding]:
    """Return the problems in the structure of curated release notes.

    Args:
        markdown: Curated release notes (parts 1 and 2 of the format).

    Returns:
        The findings, in the order of their lines; none if the notes have an
        intro paragraph, then only headings of `SECTIONS` (level 2, each
        once, in their order, none empty), holding bullets only, each ending
        with its issue and pull request numbers, and none of the parts that
        the release workflow adds (`## Commits`, the `**Full Changelog**`
        line).
    """
    findings: list[Finding] = []
    lines = markdown.splitlines()
    intro = False
    # Line of the current heading (0 in the intro), whether it is a valid
    # section, its number of bullets, and the index in `SECTIONS` of the last
    # valid section
    heading_line = 0
    valid = False
    bullets = 0
    last: int | None = None
    # Lines of the current bullet (number, text)
    item: list[tuple[int, str]] = []
    in_fence = False
    previous_blank = True

    def close_item() -> None:
        nonlocal item
        content = [(number, line) for number, line in item if line.strip()]
        # The numbers may be wrapped over lines
        text = " ".join(line.strip() for _, line in content)
        # Only in valid sections, as invalid headings have findings already
        if valid and content and not REFERENCES.fullmatch(text):
            findings.append(
                Finding(
                    content[-1][0],
                    "Bullet doesn't end with its issue and pull request"
                    " numbers, e.g., (#12, #34)",
                )
            )
        item = []

    def close_section() -> None:
        close_item()
        if valid and bullets == 0:
            findings.append(Finding(heading_line, "Empty section"))

    for number, line in enumerate(lines, start=1):
        blank = not line.strip()
        fence = FENCE.match(line) is not None
        if line.lstrip().startswith(FULL_CHANGELOG):
            findings.append(
                Finding(
                    number,
                    f"'{FULL_CHANGELOG}' line: the release workflow adds GitHub's",
                )
            )
        if in_fence or fence:
            # Code blocks belong to the intro or to a bullet
            in_fence = in_fence != fence
            if heading_line == 0:
                intro = True
            elif item:
                item.append((number, line))
            elif valid and fence and in_fence:
                findings.append(
                    Finding(
                        number,
                        "Sections hold bullets only, e.g., - <change> (#12, #34)",
                    )
                )
            previous_blank = False
            continue
        heading = HEADING.fullmatch(line)
        if heading:
            close_section()
            level = len(heading["level"])
            title = (heading["title"] or "").strip()
            finding = _heading_finding(number, level, title, last)
            if finding:
                findings.append(finding)
            elif level == 2:
                last = SECTIONS.index(title)
            heading_line, valid, bullets = number, finding is None, 0
            if not intro:
                findings.append(
                    Finding(number, "No intro paragraph before the first section")
                )
                intro = True
            previous_blank = False
            continue
        if not blank and UNDERLINE.fullmatch(line):
            findings.append(
                Finding(
                    number,
                    "Setext heading or thematic break: use no headings but the"
                    " sections, and no rules",
                )
            )
        elif heading_line == 0:
            intro = intro or not blank
        elif blank:
            if item:
                item.append((number, line))
        elif ITEM.match(line):
            close_item()
            item = [(number, line)]
            bullets += 1
        elif item and (line[0] in " \t" or not previous_blank):
            # Indented content of the bullet, or a lazy continuation line of
            # its paragraph
            item.append((number, line))
        elif valid:
            findings.append(
                Finding(
                    number, "Sections hold bullets only, e.g., - <change> (#12, #34)"
                )
            )
        previous_blank = blank
    close_section()
    if in_fence:
        findings.append(Finding(len(lines), "Unclosed fenced code block"))
    if not intro:
        findings.append(Finding(max(len(lines), 1), "No intro paragraph"))
    return sorted(findings, key=lambda finding: finding.line)


def body(curated: str, generated: str) -> str:
    """Return the body of a release: curated notes, then generated ones.

    Args:
        curated: Curated release notes (parts 1 and 2 of the format); they
            are not checked here (see `check`).
        generated: GitHub's generated notes (the `body` of the
            `generate-notes` response).

    Returns:
        The curated notes; `## Commits` with the generated notes, without
        their `## What's Changed` header (left out if they list no changes);
        and last the generated `**Full Changelog**` line.

    Raises:
        ValueError: If the generated notes don't end with a
            `**Full Changelog**` line.
    """
    lines = [line.rstrip() for line in generated.strip().splitlines()]
    if not lines or not lines[-1].startswith(f"{FULL_CHANGELOG}: "):
        raise ValueError(f"Generated notes don't end with a '{FULL_CHANGELOG}: ' line")
    if lines[0] == WHATS_CHANGED:
        lines = lines[1:]
    changes = "\n".join(lines[:-1]).strip()
    parts = [curated.strip()]
    if changes:
        parts.append(f"## {COMMITS}\n\n{changes}")
    parts.append(lines[-1])
    return "\n\n".join(parts) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.

    Returns:
        The exit status: 0 on success, 1 if the curated notes have findings or
        the generated notes are not as expected.
    """
    parser = argparse.ArgumentParser(
        description="Check curated release notes and build the release body."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="check curated notes")
    check_parser.add_argument("notes", type=Path, help="curated notes")
    body_parser = commands.add_parser("body", help="print the release body")
    body_parser.add_argument("notes", type=Path, help="curated notes")
    body_parser.add_argument(
        "generated", type=Path, help="generated notes (`body` of generate-notes)"
    )
    args = parser.parse_args(argv)
    notes: Path = args.notes
    curated = notes.read_text(encoding="utf-8")
    findings = check(curated)
    for finding in findings:
        print(
            f"::error file={notes},line={finding.line}::{finding.message}",
            file=sys.stderr,
        )
    if findings:
        return 1
    if args.command == "body":
        generated: Path = args.generated
        try:
            text = body(curated, generated.read_text(encoding="utf-8"))
        except ValueError as error:
            print(f"::error::{error}", file=sys.stderr)
            return 1
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
