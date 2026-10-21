"""Tests of the versioning of the chart's README (`scripts/chart_readme.py`)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = "https://cloud-registry.readthedocs.io/en"
REPO = "https://github.com/elixir-cloud-aai/cloud-registry"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location(
    "chart_readme", ROOT / "scripts/chart_readme.py"
)
assert _spec and _spec.loader
chart_readme = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chart_readme)


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        (
            f"See [Server]({DOCS}/latest/configuration/#server).",
            f"See [Server]({DOCS}/v1.2.3rc1/configuration/#server).",
        ),
        (f"[Docs]({DOCS}/latest/)", f"[Docs]({DOCS}/v1.2.3rc1/)"),
        (
            f"[config]({REPO}/blob/dev/cloud_registry/config.yaml)",
            f"[config]({REPO}/blob/v1.2.3rc1/cloud_registry/config.yaml)",
        ),
        # Several links per line, also in code blocks (the packaged README is
        # not rendered by the docs)
        (
            f"[a]({DOCS}/latest/a/) [b]({REPO}/blob/dev/b)\n```\n{DOCS}/latest/\n```",
            f"[a]({DOCS}/v1.2.3rc1/a/) [b]({REPO}/blob/v1.2.3rc1/b)\n"
            f"```\n{DOCS}/v1.2.3rc1/\n```",
        ),
        # Other links are left as they are
        (f"[Releases]({REPO}/releases)", f"[Releases]({REPO}/releases)"),
        (
            f"{REPO}/releases/download/vX.Y.Z/cloud-registry-X.Y.Z.tgz",
            f"{REPO}/releases/download/vX.Y.Z/cloud-registry-X.Y.Z.tgz",
        ),
        (
            "[Helm](https://helm.readthedocs.io/en/latest/)",
            "[Helm](https://helm.readthedocs.io/en/latest/)",
        ),
        (
            "[FOCA](https://github.com/elixir-cloud-aai/foca/blob/dev/README.md)",
            "[FOCA](https://github.com/elixir-cloud-aai/foca/blob/dev/README.md)",
        ),
        ("[Server](#server)", "[Server](#server)"),
    ],
)
def test_version_links(markdown: str, expected: str) -> None:
    """Links to the latest docs and to branch `dev` point to the release."""
    assert chart_readme.version_links(markdown, "v1.2.3rc1") == expected


@pytest.mark.parametrize(
    "markdown",
    [
        f"[a]({DOCS}/latest/)",
        f"{DOCS}/latest",
        "http://cloud-registry.readthedocs.io/en/latest/",
        "https://cloud-registry.readthedocs.io/latest/",
        "https://CLOUD-REGISTRY.readthedocs.io/en/latest/",
        f"[a]({REPO}/blob/dev/README.md)",
        f"{REPO}/tree/dev/deployment",
        f"{REPO}/raw/dev/deployment/values.yaml",
        f"{REPO}/blob/dev",
        "https://raw.githubusercontent.com/elixir-cloud-aai/cloud-registry/dev/x",
        "https://raw.githubusercontent.com/elixir-cloud-aai/cloud-registry/refs/heads/dev/x",
    ],
)
def test_unversioned_links(markdown: str) -> None:
    """Remaining links to the latest docs or to branch `dev` are found."""
    assert chart_readme.unversioned_links(f"Text\n{markdown}\n") == [(2, markdown)]


@pytest.mark.parametrize(
    "markdown",
    [
        f"[a]({DOCS}/v1.2.3/)",
        f"{DOCS}/latest-news/",
        f"{REPO}/blob/v1.2.3/README.md",
        f"{REPO}/blob/develop/README.md",
        f"{REPO}/releases",
        f"{REPO}/releases/download/v1.2.3/cloud-registry-1.2.3.tgz",
        "https://helm.readthedocs.io/en/latest/",
        "https://github.com/elixir-cloud-aai/foca/blob/dev/README.md",
        "The `dev` image and the latest release",
    ],
)
def test_unversioned_links_none(markdown: str) -> None:
    """Links to versions and to other sites are fine."""
    assert chart_readme.unversioned_links(markdown) == []


def test_chart_readme() -> None:
    """All links of the chart's README to the docs and files are versioned."""
    markdown = (ROOT / "deployment/README.md").read_text(encoding="utf-8")
    versioned = chart_readme.version_links(markdown, "v1.2.3")
    assert chart_readme.unversioned_links(markdown)
    assert chart_readme.unversioned_links(versioned) == []
    assert f"{DOCS}/v1.2.3/" in versioned
    assert f"{REPO}/blob/v1.2.3/" in versioned


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The README is rewritten in place."""
    readme = tmp_path / "README.md"
    readme.write_text(f"[a]({DOCS}/latest/)\n[b]({REPO}/blob/dev/b)\n")
    assert chart_readme.main([str(readme), "v1.2.3"]) == 0
    assert readme.read_text() == (f"[a]({DOCS}/v1.2.3/)\n[b]({REPO}/blob/v1.2.3/b)\n")
    assert capsys.readouterr().err == ""


def test_main_unversioned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Links left unversioned fail with an error annotation per line."""
    readme = tmp_path / "README.md"
    readme.write_text(f"[a]({DOCS}/latest/)\n[b]({REPO}/tree/dev/b)\n")
    assert chart_readme.main([str(readme), "v1.2.3"]) == 1
    assert capsys.readouterr().err == (
        f"::error file={readme},line=2::Link to the latest docs or to branch"
        f" `dev` left in the packaged README: [b]({REPO}/tree/dev/b)\n"
    )


@pytest.mark.parametrize("tag", ["1.2.3", "latest", "dev", "v1.2.3/../x", ""])
def test_main_invalid_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tag: str
) -> None:
    """Tags that are no version tags are rejected, and the README unchanged."""
    readme = tmp_path / "README.md"
    readme.write_text(f"[a]({DOCS}/latest/)\n")
    assert chart_readme.main([str(readme), tag]) == 1
    assert readme.read_text() == f"[a]({DOCS}/latest/)\n"
    assert capsys.readouterr().err == f"::error::Tag '{tag}' is not a version tag\n"
