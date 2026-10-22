"""Check that a release only pins released versions of its dependencies.

Used by the release workflow (`.github/workflows/release.yaml`), so that
temporary pins to development versions (e.g., of FOCA, to test unreleased
changes) can't be released by accident. Fails, with errors as GitHub
annotations on standard error, if:

- a dependency of the app (`project.dependencies` of `pyproject.toml`) is a
  direct reference (`<name> @ <URL>`, e.g., a Git commit);
- the base image of the `Dockerfile` (`elixircloud/foca`) has no release
  tag (`<X.Y.Z>-py<X.Y>`, e.g., a dated development image);
- a dependency of the Helm chart (`deployment/Chart.yaml`) has a pre-release
  version.

Usage: `python scripts/check_release_pins.py [<repository root>]`.

Only the stdlib is used, so the workflow runs it without installing the app.
"""

from collections.abc import Sequence
from pathlib import Path
import re
import sys
import tomllib

# A release version `X.Y.Z` (no pre-release or build metadata)
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
# A requirement that is a direct reference (PEP 508), e.g.,
# `foca @ git+https://...`, also with extras (`foca[x] @ ...`)
DIRECT_REFERENCE = re.compile(r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@")
# The base image, with its tag
BASE_IMAGE = re.compile(r"^FROM\s+elixircloud/foca:(?P<tag>[^@\s]+)", re.MULTILINE)
RELEASE_IMAGE_TAG = re.compile(SEMVER + r"-py3\.[0-9]+")
# A chart dependency's version (`version: <version>`, in a list item of
# `dependencies`); the chart is read line by line, without a YAML parser
CHART_VERSION = re.compile(r"^\s+(?:-\s+)?version:\s*[\"']?(?P<version>[^\"'\s#]+)")
RELEASE_CHART_VERSION = re.compile(SEMVER)


def _chart_dependency_versions(path: Path) -> list[str]:
    """Get the versions of the dependencies of a chart.

    The `version` keys of the `dependencies` section, i.e., of the indented
    lines after the line `dependencies:`, up to the next top-level key.

    Args:
        path: The chart's `Chart.yaml`.

    Returns:
        The versions, in order.
    """
    versions = []
    in_dependencies = False
    for line in path.read_text().splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            in_dependencies = line.rstrip() == "dependencies:"
        elif in_dependencies and (match := CHART_VERSION.match(line)):
            versions.append(match["version"])
    return versions


def find_dev_pins(root: Path) -> list[str]:
    """Find pins to development versions.

    Args:
        root: Root directory of the repository.

    Returns:
        A description of each pin to a development version.
    """
    pins = []
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    for requirement in pyproject["project"]["dependencies"]:
        if DIRECT_REFERENCE.match(requirement):
            pins.append(
                f"pyproject.toml: dependency '{requirement}' is a direct reference"
            )
    match = BASE_IMAGE.search((root / "Dockerfile").read_text())
    if match is None:
        pins.append("Dockerfile: no elixircloud/foca base image")
    elif not RELEASE_IMAGE_TAG.fullmatch(match["tag"]):
        pins.append(
            f"Dockerfile: base image tag '{match['tag']}' is not a release tag "
            "(<X.Y.Z>-py<X.Y>)"
        )
    for version in _chart_dependency_versions(root / "deployment" / "Chart.yaml"):
        if not RELEASE_CHART_VERSION.fullmatch(version):
            pins.append(
                f"deployment/Chart.yaml: dependency version '{version}' is not "
                "a release version"
            )
    return pins


def main(argv: Sequence[str] | None = None) -> int:
    """Check the pins of the repository.

    Args:
        argv: Command line arguments, without the program name: optionally,
            the root directory of the repository (default: the parent of this
            script's directory).

    Returns:
        The exit status: 0 if all pins are releases, 1 otherwise.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    root = Path(args[0]) if args else Path(__file__).resolve().parents[1]
    pins = find_dev_pins(root)
    for pin in pins:
        print(
            f"::error::{pin}: pin the released version before releasing",
            file=sys.stderr,
        )
    return 1 if pins else 0


if __name__ == "__main__":
    sys.exit(main())
