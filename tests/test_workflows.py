"""Tests for the names and the gate of the GitHub Actions workflows."""

import importlib.util
from pathlib import Path
import re
from typing import Any

from packaging.version import Version
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GATE = "passed"
# The last release signed by the release workflow before its file was renamed
# from `release.yaml` to `release.yml`
LAST_RELEASE_YAML = Version("0.4.0")

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location(
    "citation", ROOT / "scripts" / "citation.py"
)
assert _spec and _spec.loader
citation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(citation)


def _load(path: Path) -> dict[str, Any]:
    """Load a workflow file.

    Args:
        path: The path of the workflow file.

    Returns:
        The workflow.
    """
    workflow: dict[str, Any] = yaml.safe_load(path.read_text())
    return workflow


def test_workflow_files() -> None:
    """The workflows are `ci.yml`, `release.yml` and `scan.yml`."""
    names = {path.name: _load(path)["name"] for path in WORKFLOWS.iterdir()}
    assert names == {"ci.yml": "CI", "release.yml": "Release", "scan.yml": "Scan"}


def test_job_names_unique() -> None:
    """Job names are unique across all workflows (they name the checks)."""
    names = [
        job["name"]
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job in _load(path)["jobs"].values()
    ]
    assert len(names) == len(set(names))


def test_gate_needs_all_jobs() -> None:
    """The gate of `ci.yml` (the required check) needs every other job."""
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    gate = jobs[GATE]
    assert gate["name"] == "CI passed"
    assert gate["if"] == "always()"
    assert sorted(gate["needs"]) == sorted(set(jobs) - {GATE})


def test_shared_actions_pinned() -> None:
    """The shared actions of `elixir-cloud-aai/.github` are pinned to a commit.

    All pins name the same full commit SHA (bumped together), and no
    workflow uses a local copy of an action. The YAML drops comments, so the
    test below checks the release comments (`# vX.Y.Z`) in the files' text.
    """
    pinned = re.compile(r"elixir-cloud-aai/\.github/actions/[a-z-]+@([0-9a-f]{40})")
    shas: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job in _load(path)["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses", "")
                assert not uses.startswith("./"), (path.name, uses)
                if uses.startswith("elixir-cloud-aai/.github/"):
                    match = pinned.fullmatch(uses)
                    assert match, (path.name, uses)
                    shas.add(match.group(1))
    assert len(shas) == 1


def test_shared_actions_release_comments() -> None:
    """Every pin of a shared action names its release in a comment.

    Dependabot bumps a pin `@<sha> # vX.Y.Z` to new releases only if the
    pinned commit carries a version tag, and updates the comment; all pins
    name the same release. Whether the commit is the release's commit isn't
    checked here (no network in the tests).
    """
    pin = re.compile(r"uses: elixir-cloud-aai/\.github/actions/[a-z-]+@\S+(.*)$")
    comment = re.compile(r" # (v\d+\.\d+\.\d+)")
    versions: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if match := pin.search(line):
                version = comment.fullmatch(match.group(1))
                assert version, (path.name, line.strip())
                versions.add(version.group(1))
    assert len(versions) == 1, versions


def test_gate_uses_shared_action() -> None:
    """The gate of `ci.yml` uses the shared `gate` action.

    It passes the results of all needed jobs and allows `push` and
    `publish` to be skipped on pull requests only.
    """
    (step,) = _load(WORKFLOWS / "ci.yml")["jobs"][GATE]["steps"]
    assert step["uses"].startswith("elixir-cloud-aai/.github/actions/gate@")
    assert step["with"]["needs"] == "${{ toJSON(needs) }}"
    assert step["with"]["allow-skipped"] == (
        "${{ github.event_name == 'pull_request' && 'push publish' || '' }}"
    )


def test_verification_signer_workflow() -> None:
    """The verification commands name the workflow that signed the release.

    They are for the version in `CITATION.cff` (`{{ version }}`): releases up
    to 0.4.0 were signed by `release.yaml`, later ones by `release.yml`. So
    when the version is bumped past 0.4.0, the commands must name
    `release.yml`, and the note below them that 0.4.0 was signed by
    `release.yaml` (whitespace-insensitive, as the note is wrapped).
    """
    # As read by the docs (`docs/hooks.py`)
    version = citation.release_version(citation.CITATION.read_text(encoding="utf-8"))
    assert version
    text = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    signers = re.findall(r"cloud-registry/\.github/workflows/(release\.ya?ml)", text)
    # `--signer-workflow` and `--certificate-identity` of the image and the
    # chart (`gh attestation verify`, `cosign verify`, `cosign verify-blob`)
    assert len(signers) == 4
    if Version(version) <= LAST_RELEASE_YAML:
        assert set(signers) == {"release.yaml"}
    else:
        assert set(signers) == {"release.yml"}
        assert "0.4.0 was signed by `release.yaml`" in " ".join(text.split())


def test_release_chart_checked() -> None:
    """The release workflow checks the chart before attesting and signing it.

    `publish` downloads the chart artifact, which any job of the run could
    replace, so it checks that `dist/` holds exactly the file that `prepare`
    packaged (its name and SHA-256, as outputs of `prepare`) before the chart
    is attested, signed and released.
    """
    release = _load(WORKFLOWS / "release.yml")["jobs"]
    prepare = release["prepare"]
    assert prepare["outputs"]["chart_file"] == "${{ steps.chart.outputs.file }}"
    assert prepare["outputs"]["chart_sha256"] == "${{ steps.chart.outputs.sha256 }}"
    packaging = {step["name"]: step for step in prepare["steps"]}
    package = packaging["Package chart"]
    assert package["id"] == "chart"
    assert 'echo "file=${package#dist/}" >> "${GITHUB_OUTPUT}"' in package["run"]
    assert 'sha256sum "${package}"' in package["run"]
    # Re-runs of all jobs replace the chart, so its checksum is the new one
    assert packaging["Upload chart"]["with"]["overwrite"] is True
    steps = {step["name"]: step for step in release["publish"]["steps"]}
    check = steps["Check chart"]
    assert check["env"]["FILE"] == "${{ needs.prepare.outputs.chart_file }}"
    assert check["env"]["SHA256"] == "${{ needs.prepare.outputs.chart_sha256 }}"
    assert r'[[ ! "${FILE}" =~ ^cloud-registry-[0-9A-Za-z.+-]+\.tgz$ ]]' in check["run"]
    assert '[[ ! "${SHA256}" =~ ^[0-9a-f]{64}$ ]]' in check["run"]
    assert '"${#files[@]}" -ne 1' in check["run"]
    assert '"${files[0]}" != "dist/${FILE}"' in check["run"]
    assert "sha256sum --check --strict" in check["run"]
    names = list(steps)
    assert names.index("Download chart") < names.index("Check chart")
    assert names.index("Check chart") < names.index("Attest chart provenance")
    assert names.index("Check chart") < names.index("Sign chart")
    assert names.index("Sign chart") < names.index("Create release")
