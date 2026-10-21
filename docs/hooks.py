"""MkDocs hooks: include the README of the Helm chart in the deployment page.

The chart's `deployment/README.md` is kept next to the chart and is written to
be read on GitHub, so its relative links point from `deployment/` to files of
the repository. This hook replaces the line `--8<-- "deployment/README.md"` in
pages with the README, with its links adapted to the docs: links to pages of
the docs (`../docs/<page>.md`) become links to these pages, which MkDocs then
checks, and links to other files of the repository become links to them on
GitHub (`repo_url`, at the commit that Read the Docs builds, so that the
files match the version of the docs, otherwise at branch `dev`). Only inline
links (`[text](target)`) outside fenced code blocks are adapted; use no other
link forms in the README (the strict build fails on them, as their targets
aren't found), and no nested fences (every line starting with ``` or ~~~
opens or closes a block).
"""

import os
from pathlib import Path
import re

INCLUDE = '--8<-- "deployment/README.md"'
README = Path("deployment/README.md")

# Targets of inline links (`[text](target)`)
LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
# Targets with a URL scheme (e.g., `https:`, `mailto:`)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Opening and closing lines of fenced code blocks
FENCE = re.compile(r"^\s*(```|~~~)")


def adapt_link(target: str, repo_url: str, ref: str = "dev") -> str:
    """Return the target of a link of the chart's README for the docs.

    Args:
        target: Link target in `deployment/README.md`.
        repo_url: URL of the repository on GitHub.
        ref: Git reference (branch, tag or commit) of files on GitHub.

    Returns:
        The target relative to the page in the docs (`docs/<page>.md`), or the
        URL of the file on GitHub; unchanged for anchors and absolute URLs.
    """
    if target.startswith("#") or SCHEME.match(target):
        return target
    if target.startswith("../docs/"):
        return target.removeprefix("../docs/")
    if target.startswith("../"):
        path = target.removeprefix("../")
    else:
        path = f"deployment/{target}"
    return f"{repo_url.rstrip('/')}/blob/{ref}/{path}"


def adapt_links(markdown: str, repo_url: str, ref: str = "dev") -> str:
    """Adapt the inline links outside fenced code blocks (see `adapt_link`)."""
    lines = []
    in_fence = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            line = LINK.sub(
                lambda match: f"]({adapt_link(match['target'], repo_url, ref)})",
                line,
            )
        lines.append(line)
    return "\n".join(lines)


def on_page_markdown(markdown, page, config, files):
    """Include the chart's README in pages that ask for it."""
    if INCLUDE not in markdown.splitlines():
        return markdown
    readme = Path(config.config_file_path).parent / README
    # Set by Read the Docs to the commit of the build
    ref = os.environ.get("READTHEDOCS_GIT_COMMIT_HASH") or "dev"
    content = adapt_links(readme.read_text(encoding="utf-8"), config.repo_url, ref)
    return "\n".join(
        content if line == INCLUDE else line for line in markdown.splitlines()
    )
