"""Tests of the MkDocs hooks (`docs/hooks.py`)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/elixir-cloud-aai/cloud-registry"
DOCS_URL = "https://cloud-registry.readthedocs.io/en/latest/"

# `docs/` is not a package, so the hooks are loaded from their file
_spec = importlib.util.spec_from_file_location("docs_hooks", ROOT / "docs/hooks.py")
assert _spec and _spec.loader
hooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hooks)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("#names-and-labels", "#names-and-labels"),
        ("https://helm.sh/docs/", "https://helm.sh/docs/"),
        ("mailto:admin@example.org", "mailto:admin@example.org"),
        (f"{DOCS_URL}configuration/#server", "configuration.md#server"),
        (f"{DOCS_URL}releases/0.3.0/", "releases/0.3.0.md"),
        (f"{DOCS_URL}operations", "operations.md"),
        (DOCS_URL, "index.md"),
        (f"{DOCS_URL}#contact", "index.md#contact"),
        (
            f"{REPO_URL}/blob/dev/cloud_registry/config.yaml",
            f"{REPO_URL}/blob/dev/cloud_registry/config.yaml",
        ),
        (f"{REPO_URL}/releases", f"{REPO_URL}/releases"),
        # Relative links are left as they are, so that the strict build fails
        ("values.yaml", "values.yaml"),
    ],
)
def test_adapt_link(target, expected):
    """Links are adapted to pages of the docs, or to files on GitHub."""
    assert hooks.adapt_link(target, f"{REPO_URL}/") == expected


def test_adapt_link_ref():
    """Links to files on GitHub point to the given Git reference."""
    assert (
        hooks.adapt_link(
            f"{REPO_URL}/blob/dev/deployment/values.yaml", REPO_URL, "0123abc"
        )
        == f"{REPO_URL}/blob/0123abc/deployment/values.yaml"
    )


def test_adapt_links_skips_code_blocks():
    """Links in fenced code blocks are left as they are."""
    link = f"{DOCS_URL}configuration/"
    markdown = f"See [a]({link}).\n\n```bash\necho '[b]({link})'\n```\n"
    assert hooks.adapt_links(markdown, REPO_URL).splitlines() == [
        "See [a](configuration.md).",
        "",
        "```bash",
        f"echo '[b]({link})'",
        "```",
    ]


def test_readme_links_are_absolute():
    """The chart's README has no relative links (they break in the package)."""
    readme = (ROOT / "deployment/README.md").read_text(encoding="utf-8")
    targets = hooks.LINK.findall(readme)
    assert f"{DOCS_URL}releases/0.3.0/" in targets
    assert [
        target for target in targets if not target.startswith(("#", "https://"))
    ] == []


def _render(markdown):
    config = SimpleNamespace(
        config_file_path=str(ROOT / "mkdocs.yml"), repo_url=REPO_URL
    )
    return hooks.on_page_markdown(markdown, page=None, config=config, files=None)


def test_on_page_markdown_includes_readme(monkeypatch):
    """The include line is replaced with the chart's README."""
    monkeypatch.delenv("READTHEDOCS_GIT_COMMIT_HASH", raising=False)
    markdown = _render(f"<!-- comment -->\n{hooks.INCLUDE}\n")
    assert markdown.startswith("<!-- comment -->\n# Helm chart\n")
    assert hooks.INCLUDE not in markdown
    assert f"]({DOCS_URL}" not in markdown
    assert "](releases/0.3.0.md)" in markdown
    assert f"]({REPO_URL}/blob/dev/cloud_registry/config.yaml)" in markdown


def test_on_page_markdown_read_the_docs_commit(monkeypatch):
    """On Read the Docs, links to files point to the commit of the build."""
    monkeypatch.setenv("READTHEDOCS_GIT_COMMIT_HASH", "0123abc")
    markdown = _render(f"{hooks.INCLUDE}\n")
    assert f"]({REPO_URL}/blob/0123abc/cloud_registry/config.yaml)" in markdown


def test_on_page_markdown_without_include():
    """Other pages are left as they are."""
    markdown = f"# Page\n\nSee `{hooks.INCLUDE}`.\n"
    assert _render(markdown) == markdown
