"""Point the links of the chart's README to the docs and files of a release.

`deployment/README.md` links to the latest docs on Read the Docs (`DOCS`) and
to files on branch `dev` (`FILES`), which is right for the repository and the
docs (`docs/hooks.py`), but not for the README packaged with a released chart:
its users would read docs of newer versions. The release workflow
(`.github/workflows/release.yaml`) runs this script on a copy of the chart
before packaging it: it rewrites these links to the release's docs and files
(`/en/v<version>/`, `blob/v<version>/`; Read the Docs builds version tags),
and fails with GitHub annotations on standard error if any link to the latest
docs or to branch `dev` is left. See "Versioning and releases" in
`docs/development.md`.

Usage: `python scripts/chart_readme.py <readme> <tag>`, with the release tag,
e.g., `v1.2.3`. Only the stdlib is used, so the workflow runs it without
installing the app.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path
import re
import sys

# Links that are rewritten, without the version
DOCS = "https://cloud-registry.readthedocs.io/en/"
FILES = "https://github.com/elixir-cloud-aai/cloud-registry/blob/"

# Links to the latest docs or to branch `dev` (also forms that aren't
# rewritten), which must not be left in the packaged README
UNVERSIONED = re.compile(
    r"cloud-registry\.readthedocs\.io/(?:en/)?latest(?![\w.-])"
    r"|github\.com/elixir-cloud-aai/cloud-registry/(?:blob|tree|raw)/dev(?![\w.-])"
    r"|raw\.githubusercontent\.com/elixir-cloud-aai/cloud-registry/"
    r"(?:refs/heads/)?dev(?![\w.-])",
    re.ASCII | re.IGNORECASE,
)
# Version tags (`v` and a version), which are used in URL paths
VERSION_TAG = re.compile(r"v[0-9]+(?:\.[0-9]+)*[0-9a-z]*", re.ASCII)


def version_links(markdown: str, tag: str) -> str:
    """Return Markdown with its links pointed to the docs and files of a tag.

    Args:
        markdown: Markdown, e.g., of the chart's README.
        tag: Git tag of the release, e.g., `v1.2.3`.

    Returns:
        The Markdown with links to the latest docs (`DOCS` + `latest/`) and to
        files on branch `dev` (`FILES` + `dev/`) pointed to `tag`, also in
        code blocks; other links unchanged.
    """
    return markdown.replace(f"{DOCS}latest/", f"{DOCS}{tag}/").replace(
        f"{FILES}dev/", f"{FILES}{tag}/"
    )


def unversioned_links(markdown: str) -> list[tuple[int, str]]:
    """Return the lines with links to the latest docs or to branch `dev`.

    Args:
        markdown: Markdown, e.g., of the chart's README.

    Returns:
        Number (from 1) and text of each line with such a link (`UNVERSIONED`).
    """
    return [
        (number, line)
        for number, line in enumerate(markdown.splitlines(), start=1)
        if UNVERSIONED.search(line)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Point the links of a README to a release (see the module's docstring).

    Args:
        argv: Command-line arguments, without the program name; those of the
            process if `None`.

    Returns:
        The exit status: 0 on success, 1 if the tag is not a version tag (the
        README is left unchanged) or links to the latest docs or to branch
        `dev` are left.
    """
    parser = argparse.ArgumentParser(
        description="Point the links of the chart's README to a release."
    )
    parser.add_argument("readme", type=Path, help="README to rewrite in place")
    parser.add_argument("tag", help="release tag, e.g., v1.2.3")
    args = parser.parse_args(argv)
    if not VERSION_TAG.fullmatch(args.tag):
        print(f"::error::Tag '{args.tag}' is not a version tag", file=sys.stderr)
        return 1
    readme: Path = args.readme
    markdown = version_links(readme.read_text(encoding="utf-8"), args.tag)
    readme.write_text(markdown, encoding="utf-8")
    left = unversioned_links(markdown)
    for number, line in left:
        print(
            f"::error file={readme},line={number}::Link to the latest docs or to"
            f" branch `dev` left in the packaged README: {line.strip()}",
            file=sys.stderr,
        )
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
