"""Tests of the MkDocs hooks (`docs/hooks.py`)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

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
        (f"{DOCS_URL}releases/1.2.3/", "releases/1.2.3.md"),
        (f"{DOCS_URL}operations", "operations.md"),
        (DOCS_URL, "index.md"),
        (f"{DOCS_URL}#contact", "index.md#contact"),
        # Only links to branch `dev` are pointed to the given reference
        (
            f"{REPO_URL}/blob/v1.2.3/cloud_registry/config.yaml",
            f"{REPO_URL}/blob/v1.2.3/cloud_registry/config.yaml",
        ),
        (f"{REPO_URL}/releases", f"{REPO_URL}/releases"),
        # Relative links are left as they are, so that the strict build fails
        ("values.yaml", "values.yaml"),
    ],
)
def test_adapt_link(target: str, expected: str) -> None:
    """Links are adapted to pages of the docs, or to files on GitHub."""
    assert hooks.adapt_link(target, f"{REPO_URL}/") == expected


@pytest.mark.parametrize("repo_url", [REPO_URL, f"{REPO_URL}/"])
def test_adapt_link_ref(repo_url: str) -> None:
    """Links to files on GitHub point to the given Git reference."""
    assert (
        hooks.adapt_link(
            f"{REPO_URL}/blob/dev/deployment/values.yaml", repo_url, "0123abc"
        )
        == f"{REPO_URL}/blob/0123abc/deployment/values.yaml"
    )


def test_adapt_links_skips_code_blocks() -> None:
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


def _is_readthedocs(url: str) -> bool:
    """Tell whether a URL points to a site on Read the Docs.

    Args:
        url: URL to check.

    Returns:
        Whether the URL's host is `readthedocs.io` or one of its subdomains,
        also if spelled with percent-encodings or a trailing dot.
    """
    host = unquote(urlsplit(url).hostname or "").rstrip(".")
    return host == "readthedocs.io" or host.endswith(".readthedocs.io")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (DOCS_URL, True),
        ("https://readthedocs.io/", True),
        ("https://cloud-registry.readthedocs.io./en/0.2.0/", True),
        ("https://cloud-registry.readthedocs%2Eio/en/0.2.0/", True),
        ("https://readthedocs.io.example.com/", False),
        ("https://example.com/readthedocs.io/", False),
        ("#readthedocs.io", False),
    ],
)
def test_is_readthedocs(url: str, expected: bool) -> None:
    """Only URLs on the Read the Docs host count as links to the docs.

    Args:
        url: URL to check.
        expected: Whether the URL is on Read the Docs.
    """
    assert _is_readthedocs(url) is expected


def test_readme_links_are_absolute() -> None:
    """The chart's README has no relative links (they break in the package)."""
    readme = (ROOT / "deployment/README.md").read_text(encoding="utf-8")
    targets = hooks.LINK.findall(readme)
    assert f"{DOCS_URL}changelog/" in targets
    assert [
        target for target in targets if not target.startswith(("#", "https://"))
    ] == []
    # Links to the docs are to the version that the hook adapts
    assert [
        target
        for target in targets
        if _is_readthedocs(target) and not target.startswith(DOCS_URL)
    ] == []


def _render(markdown: str) -> str:
    """Run the hook on the Markdown of a page, with the repository's settings.

    Args:
        markdown: Markdown of the page.

    Returns:
        The Markdown returned by the hook.
    """
    config = SimpleNamespace(
        config_file_path=str(ROOT / "mkdocs.yml"), repo_url=REPO_URL
    )
    # the hooks are loaded from their file, so their types are unknown
    rendered: str = hooks.on_page_markdown(
        markdown, page=None, config=config, files=None
    )
    return rendered


def test_on_page_markdown_includes_readme(monkeypatch: pytest.MonkeyPatch) -> None:
    """The include line is replaced with the chart's README."""
    monkeypatch.delenv("READTHEDOCS_GIT_COMMIT_HASH", raising=False)
    markdown = _render(f"<!-- comment -->\n{hooks.INCLUDE}\n")
    assert markdown.startswith("<!-- comment -->\n# Helm chart\n")
    assert hooks.INCLUDE not in markdown
    assert f"]({DOCS_URL}" not in markdown
    assert "](changelog.md)" in markdown
    assert f"]({REPO_URL}/blob/dev/cloud_registry/config.yaml)" in markdown


def test_on_page_markdown_read_the_docs_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Read the Docs, links to files point to the commit of the build."""
    monkeypatch.setenv("READTHEDOCS_GIT_COMMIT_HASH", "0123abc")
    markdown = _render(f"{hooks.INCLUDE}\n")
    assert f"]({REPO_URL}/blob/0123abc/cloud_registry/config.yaml)" in markdown


def test_on_page_markdown_without_include() -> None:
    """Other pages are left as they are."""
    markdown = f"# Page\n\nSee `{hooks.INCLUDE}`.\n"
    assert _render(markdown) == markdown


def _version() -> str:
    """Return the release version of the repository's citation metadata.

    Returns:
        The `version` of `CITATION.cff`.
    """
    version = hooks.citation.release_version(
        (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    )
    assert isinstance(version, str)
    return version


def test_on_page_markdown_version() -> None:
    """The placeholder is the release version, also in code blocks."""
    version = _version()
    markdown = _render(
        "Version {{ version }}.\n\n```bash\ndocker pull x:{{ version }}\n```\n"
        "{{ json .SBOM }} {{version}}\n"
    )
    assert markdown == (
        f"Version {version}.\n\n```bash\ndocker pull x:{version}\n```\n"
        "{{ json .SBOM }} {{version}}\n"
    )


def test_on_page_markdown_readme_version() -> None:
    """The placeholder is also replaced in the included chart README."""
    readme = (ROOT / "deployment/README.md").read_text(encoding="utf-8")
    assert "{{ version }}" in readme
    markdown = _render(f"{hooks.INCLUDE}\n")
    assert "{{ version }}" not in markdown
    assert f"cloud-registry:{_version()}`" in markdown


@pytest.mark.parametrize(
    ("markdown", "version", "expected"),
    [
        ("a {{ version }} b {{ version }}", "1.2.3", "a 1.2.3 b 1.2.3"),
        ("{{ version }} {{ chart_version }}", "1.2.3rc1", "1.2.3rc1 1.2.3-rc1"),
        ("only {{ chart_version }}", "1.2.3b1", "only 1.2.3-b1"),
        ("no placeholder", None, "no placeholder"),
    ],
)
def test_substitute_version(markdown: str, version: str | None, expected: str) -> None:
    """The placeholder is replaced; without it, no version is needed.

    Args:
        markdown: Markdown of a page.
        version: Release version.
        expected: Expected Markdown.
    """
    assert hooks.substitute_version(markdown, version) == expected


def test_substitute_version_without_version() -> None:
    """Pages using the placeholder fail the build without a version."""
    for markdown in ["{{ version }}", "{{ chart_version }}"]:
        with pytest.raises(ValueError, match="has no version"):
            hooks.substitute_version(markdown, None)
