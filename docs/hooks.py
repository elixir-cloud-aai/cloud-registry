"""MkDocs hooks: include the README of the Helm chart in the deployment page.

The chart's `deployment/README.md` is kept next to the chart and is written to
be read on GitHub, so its relative links point from `deployment/` to files of
the repository. This hook replaces the line `--8<-- "deployment/README.md"` in
pages with the README, with its links adapted to the docs: links to pages of
the docs (`../docs/<page>.md`) become links to these pages, which MkDocs then
checks, and links to other files of the repository become links to them on
GitHub.
"""

from pathlib import Path
import re

INCLUDE = '--8<-- "deployment/README.md"'
README = Path("deployment/README.md")
REPOSITORY = "https://github.com/elixir-cloud-aai/cloud-registry"

# Targets of inline links (`[text](target)`)
LINK = re.compile(r"\]\((?P<target>[^)\s]+)\)")
# Targets with a URL scheme (e.g., `https:`, `mailto:`)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def adapt_link(target: str) -> str:
    """Return the target of a link of the chart's README for the docs.

    Args:
        target: Link target in `deployment/README.md`.

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
    return f"{REPOSITORY}/blob/dev/{path}"


def on_page_markdown(markdown, page, config, files):
    """Include the chart's README in pages that ask for it."""
    if INCLUDE not in markdown.splitlines():
        return markdown
    readme = Path(config.config_file_path).parent / README
    content = LINK.sub(
        lambda match: f"]({adapt_link(match['target'])})",
        readme.read_text(encoding="utf-8"),
    )
    return "\n".join(
        content if line == INCLUDE else line for line in markdown.splitlines()
    )
