"""Tests of the MkDocs hooks (`docs/hooks.py`)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/elixir-cloud-aai/cloud-registry"

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
        ("../docs/configuration.md#server", "configuration.md#server"),
        ("../docs/releases/0.3.0.md", "releases/0.3.0.md"),
        (
            "../cloud_registry/config.yaml",
            f"{REPO_URL}/blob/dev/cloud_registry/config.yaml",
        ),
        ("values.yaml", f"{REPO_URL}/blob/dev/deployment/values.yaml"),
    ],
)
def test_adapt_link(target, expected):
    """Links are adapted to the docs, or to files on GitHub."""
    assert hooks.adapt_link(target, f"{REPO_URL}/") == expected


def test_adapt_link_ref():
    """Links to files on GitHub point to the given Git reference."""
    assert (
        hooks.adapt_link("values.yaml", REPO_URL, "0123abc")
        == f"{REPO_URL}/blob/0123abc/deployment/values.yaml"
    )


def test_adapt_links_skips_code_blocks():
    """Links in fenced code blocks are left as they are."""
    markdown = "See [a](values.yaml).\n\n```bash\necho '[b](values.yaml)'\n```\n"
    assert hooks.adapt_links(markdown, REPO_URL).splitlines() == [
        f"See [a]({REPO_URL}/blob/dev/deployment/values.yaml).",
        "",
        "```bash",
        "echo '[b](values.yaml)'",
        "```",
    ]


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
    assert "](../" not in markdown
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
