"""Check that a release only pins released versions of its dependencies.

Used by the release workflow (`.github/workflows/release.yaml`), so that
temporary pins to development versions (e.g., of FOCA, to test unreleased
changes) can't be released by accident. Fails, with errors as GitHub
annotations on standard error, if:

- a requirement of `pyproject.toml` (`project.dependencies`,
  `project.optional-dependencies`, `dependency-groups` or
  `build-system.requires`) is a direct reference (`<name> @ <URL>`, e.g., a
  Git commit) or names a pre-release or development version (e.g.,
  `>=0.17.0rc1`);
- a base image of the `Dockerfile` (`elixircloud/foca`, in any stage) has
  no release tag (`<X.Y.Z>-py<X.Y>`, e.g., a dated development image);
- a dependency of the Helm chart (`deployment/Chart.yaml`) has no version or
  another version than a release (`X.Y.Z`, e.g., a pre-release or a range),
  has no chart repository (`oci://` or `https://`, e.g., `file://` or an
  alias), or is given in flow style (`{...}`, `[...]`), which the check
  can't read.

Usage: `python scripts/check_release_pins.py [<repository root>]`.

Only the stdlib is used, so the workflow runs it without installing the app.
"""

from collections.abc import Iterable, Sequence
from pathlib import Path
import re
import sys
import tomllib

# A release version `X.Y.Z` (no pre-release or build metadata)
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
# The name of a requirement (PEP 508), with extras (`foca[x]`)
NAME = r"^\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?"
# A requirement that is a direct reference, e.g., `foca @ git+https://...`
DIRECT_REFERENCE = re.compile(NAME + r"\s*@")
# A pre-release or development version (PEP 440) in a version specifier,
# e.g., `0.17.0rc1`, `1.0.dev0`
PRE_RELEASE = re.compile(
    r"[0-9][._-]?(a|b|c|rc|alpha|beta|pre|preview|dev)[0-9]*(?![a-z])",
    re.IGNORECASE,
)
# A base image of FOCA, with its tag if it has one, in any stage, also with
# options (`--platform=...`) or the registry (`docker.io/`)
BASE_IMAGE = re.compile(
    r"^[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*(?:docker\.io/)?elixircloud/foca"
    r"(?::(?P<tag>[^@\s]+))?(?![^@\s])",
    re.IGNORECASE | re.MULTILINE,
)
RELEASE_IMAGE_TAG = re.compile(SEMVER + r"-py3\.[0-9]+")
# A key of a chart dependency (`<key>: <value>`, optionally starting a list
# item), up to a comment; the chart is read line by line, without a YAML
# parser
CHART_KEY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<item>-[ \t]+)?(?P<key>[A-Za-z][A-Za-z0-9_-]*):"
    r"[ \t]*[\"']?(?P<value>.*?)[\"']?[ \t]*(?:#.*)?$"
)
# The start of a list item, up to its content; group `flow` if the item is
# in flow style
LIST_ITEM = re.compile(r"[ \t]*-(?:[ \t]+|$)(?P<flow>[{\[])?")
RELEASE_CHART_VERSION = re.compile(SEMVER)
CHART_REPOSITORY = re.compile(r"(oci|https)://\S+")


def _chart_dependencies(path: Path) -> list[dict[str, str] | None]:
    """Get the dependencies of a chart.

    The list items of the `dependencies` section, i.e., of the lines after
    the line `dependencies:` that are indented or list items, up to the next
    top-level key, with their keys (those at the indentation of each item's
    first key; nested keys are left out).

    Args:
        path: The chart's `Chart.yaml`.

    Returns:
        The dependencies, in order, as mappings of keys to values; `None`
        for dependencies in flow style (`{...}`, `[...]`), which aren't
        read.
    """
    dependencies: list[dict[str, str] | None] = []
    in_dependencies = False
    item_indent: int | None = None
    key_indent = 0
    for line in path.read_text().splitlines():
        if line and line[0] not in " \t-#":
            key, _, value = line.split("#", 1)[0].partition(":")
            in_dependencies = key.strip() == "dependencies"
            item_indent = None
            if in_dependencies and value.strip():
                dependencies.append(None)
            continue
        if not in_dependencies or not line.strip() or line.lstrip()[0] == "#":
            continue
        indent = len(line) - len(line.lstrip())
        item = LIST_ITEM.match(line)
        if item and (item_indent is None or indent == item_indent):
            item_indent = indent
            if item["flow"]:
                dependencies.append(None)
                continue
            dependencies.append({})
            key_indent = item.end()
        match = CHART_KEY.match(line)
        current = dependencies[-1] if dependencies else None
        if match and current is not None:
            start = len(match["indent"]) + len(match["item"] or "")
            if start == key_indent or not current:
                key_indent = start
                current[match["key"]] = match["value"]
    return dependencies


def _requirement_pins(requirements: Iterable[object], kind: str) -> list[str]:
    """Find requirements of `pyproject.toml` on development versions.

    Args:
        requirements: The requirements (PEP 508); other entries (e.g.,
            includes of dependency groups) are skipped.
        kind: What they are, for the descriptions (e.g., `dependency`).

    Returns:
        A description of each pin to a development version.
    """
    pins = []
    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        if DIRECT_REFERENCE.match(requirement):
            pins.append(f"pyproject.toml: {kind} '{requirement}' is a direct reference")
        elif PRE_RELEASE.search(re.sub(NAME, "", requirement).split(";")[0]):
            pins.append(f"pyproject.toml: {kind} '{requirement}' names a pre-release")
    return pins


def find_dev_pins(root: Path) -> list[str]:
    """Find pins to development versions.

    Args:
        root: Root directory of the repository.

    Returns:
        A description of each pin to a development version.
    """
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    project = pyproject.get("project", {})
    pins = [
        *_requirement_pins(
            [
                *project.get("dependencies", []),
                *(
                    requirement
                    for extra in project.get("optional-dependencies", {}).values()
                    for requirement in extra
                ),
                *(
                    requirement
                    for group in pyproject.get("dependency-groups", {}).values()
                    for requirement in group
                ),
            ],
            "dependency",
        ),
        *_requirement_pins(
            pyproject.get("build-system", {}).get("requires", []),
            "build requirement",
        ),
    ]
    images = list(BASE_IMAGE.finditer((root / "Dockerfile").read_text()))
    if not images:
        pins.append("Dockerfile: no elixircloud/foca base image")
    for image in images:
        if image["tag"] is None:
            pins.append("Dockerfile: base image has no tag (<X.Y.Z>-py<X.Y>)")
        elif not RELEASE_IMAGE_TAG.fullmatch(image["tag"]):
            pins.append(
                f"Dockerfile: base image tag '{image['tag']}' is not a release tag "
                "(<X.Y.Z>-py<X.Y>)"
            )
    for dependency in _chart_dependencies(root / "deployment" / "Chart.yaml"):
        if dependency is None:
            pins.append(
                "deployment/Chart.yaml: dependencies in flow style can't be checked"
            )
            continue
        name = dependency.get("name", "")
        version = dependency.get("version")
        repository = dependency.get("repository", "")
        if version is None:
            pins.append(f"deployment/Chart.yaml: dependency '{name}' has no version")
        elif not RELEASE_CHART_VERSION.fullmatch(version):
            pins.append(
                f"deployment/Chart.yaml: dependency version '{version}' is not "
                "a release version"
            )
        if not CHART_REPOSITORY.fullmatch(repository):
            pins.append(
                f"deployment/Chart.yaml: dependency '{name}' has repository "
                f"'{repository}', not a chart repository (oci:// or https://)"
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
