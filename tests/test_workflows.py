"""Tests for the GitHub Actions workflows (names, gate, release checks)."""

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib
from typing import Any

from packaging.version import Version
import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
GATE = "passed"
# The last release signed by the release workflow before its file was renamed
# from `release.yaml` to `release.yml`
LAST_RELEASE_YAML = Version("0.4.0")
# A chart file name as the release workflow packages it
CHART = "cloud-registry-1.2.3.tgz"

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
    is attested, signed and released. The wiring and order of the steps are
    checked here, the script itself by `test_release_chart_check_runs`.
    """
    release = _load(WORKFLOWS / "release.yml")["jobs"]
    prepare = release["prepare"]
    assert prepare["outputs"]["chart_file"] == "${{ steps.chart.outputs.file }}"
    assert prepare["outputs"]["chart_sha256"] == "${{ steps.chart.outputs.sha256 }}"
    packaging = {step["name"]: step for step in prepare["steps"]}
    assert packaging["Package chart"]["id"] == "chart"
    # Re-runs of all jobs replace the chart, so its checksum is the new one
    assert packaging["Upload chart"]["with"]["overwrite"] is True
    steps = {step["name"]: step for step in release["publish"]["steps"]}
    check = steps["Check chart"]
    assert check["env"]["FILE"] == "${{ needs.prepare.outputs.chart_file }}"
    assert check["env"]["SHA256"] == "${{ needs.prepare.outputs.chart_sha256 }}"
    # What the script accepts is tested by `test_release_chart_check_runs`
    # Only the checked file is attested and signed
    chart = "dist/${{ needs.prepare.outputs.chart_file }}"
    assert steps["Attest chart provenance"]["with"]["subject-path"] == chart
    assert steps["Sign chart"]["env"]["CHART"] == chart
    assert "*" not in steps["Sign chart"]["run"]
    names = list(steps)
    assert names.index("Download chart") < names.index("Check chart")
    # A rejected chart leaves the image (pushed untagged by `push`) without
    # GitHub attestation, signature and tags too
    assert names.index("Check chart") < names.index("Attest build provenance")
    assert names.index("Check chart") < names.index("Sign image")
    assert names.index("Check chart") < names.index("Tag image")
    assert names.index("Check chart") < names.index("Attest chart provenance")
    assert names.index("Check chart") < names.index("Sign chart")
    assert names.index("Sign chart") < names.index("Create release")


@pytest.mark.parametrize(
    ("extra", "rename", "tamper", "file", "sha256", "passes"),
    [
        pytest.param(None, None, False, CHART, None, True, id="good"),
        pytest.param(None, None, True, CHART, None, False, id="wrong-hash"),
        pytest.param("other.tgz", None, False, CHART, None, False, id="extra"),
        pytest.param(".hidden.tgz", None, False, CHART, None, False, id="hidden"),
        pytest.param(
            None, "cloud-registry-9.9.9.tgz", False, CHART, None, False, id="renamed"
        ),
        pytest.param(None, None, False, "../x.tgz", None, False, id="bad-name"),
        pytest.param(None, None, False, CHART, "abc", False, id="bad-hash"),
    ],
)
def test_release_chart_check_runs(
    tmp_path: Path,
    extra: str | None,
    rename: str | None,
    tamper: bool,
    file: str,
    sha256: str | None,
    passes: bool,
) -> None:
    """The chart check of `publish` passes only for the chart of `prepare`.

    The step's script runs with the options of GitHub's default shell for
    steps without `shell:` (`bash -e`, no `pipefail`) in a directory whose
    `dist/` holds the downloaded artifact. It needs `bash`
    and GNU `sha256sum`, as on the GitHub runners.

    Args:
        tmp_path: The working directory of the step.
        extra: A file added to the artifact, if any.
        rename: The name the chart is renamed to in the artifact, if any.
        tamper: Whether the chart's content is changed after packaging.
        file: The chart file name output by `prepare`.
        sha256: The checksum output by `prepare` (that of the chart if None).
        passes: Whether the check passes.
    """
    bash = shutil.which("bash")
    assert bash
    steps = _load(WORKFLOWS / "release.yml")["jobs"]["publish"]["steps"]
    script = {step["name"]: step for step in steps}["Check chart"]["run"]
    dist = tmp_path / "dist"
    dist.mkdir()
    chart = dist / CHART
    chart.write_bytes(b"chart")
    digest = hashlib.sha256(chart.read_bytes()).hexdigest()
    if tamper:
        chart.write_bytes(b"other chart")
    if rename:
        chart.rename(dist / rename)
    if extra:
        (dist / extra).write_bytes(b"extra")
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [bash, "--noprofile", "--norc", "-e", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "FILE": file, "SHA256": sha256 or digest},
        check=False,
    )
    assert (result.returncode == 0) == passes, result.stdout + result.stderr


def _credential_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """List the jobs that hold publishing credentials.

    These are the jobs of all workflows that run in an environment
    (`environment:`, which holds the Docker Hub secrets), use a Docker Hub
    secret directly, or may sign or write (`id-token`, `contents`,
    `packages` or `attestations` permission `write`, or `write-all`, also
    inherited from the workflow's permissions). No workflow passes a
    secret to all its jobs (workflow-level `env:`).

    Returns:
        The workflow file name, job ID and job of each such job.
    """
    jobs = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _load(path)
        assert "secrets." not in yaml.safe_dump(workflow.get("env", {})), path
        for job_id, job in workflow["jobs"].items():
            # Jobs without permissions of their own inherit the workflow's
            permissions = job.get("permissions", workflow.get("permissions", {}))
            writes = permissions == "write-all" or (
                isinstance(permissions, dict)
                and any(
                    permissions.get(scope) == "write"
                    for scope in ("id-token", "contents", "packages", "attestations")
                )
            )
            if (
                "environment" in job
                or "secrets.DOCKERHUB" in yaml.safe_dump(job)
                or writes
            ):
                jobs.append((path.name, job_id, job))
    return jobs


def _commands(step: dict[str, Any]) -> list[str]:
    """Split the script of a step into commands.

    Args:
        step: A step of a job.

    Returns:
        The commands of the step's `run` script: its lines, with
        continuation lines joined, split at `;`, `&&`, `||` and `|`, with
        whitespace normalised.
    """
    lines = step.get("run", "").replace("\\\n", " ").splitlines()
    return [
        " ".join(command.split())
        for line in lines
        for command in re.split(r"&&|\|\||;|\|", line)
        if command.strip()
    ]


# pip (also `pip3`, `pip3.13`, `"<venv>/bin/pip"`, with global options and
# their values) commands that install or build packages; the latter runs
# their build backends
PIP = re.compile(r"\bpip[0-9.]*[\"']?(\s+\S+)*?\s+(install|wheel|download)\b")
# The only options of an install in a job with credentials, besides `-r`
HASH_CHECKED = ["--require-hashes", "--only-binary", ":all:", "--no-deps"]
# Other installers, and builds that install build requirements
INSTALLERS = re.compile(
    r"\b(pipx|pipenv|easy_install|uvx|pip-sync|poetry|pdm|hatch"
    r"|uv\s+(pip|tool|sync|run|add|build|venv)|python[0-9.]*\s+-m\s+build)\b"
)


def test_credential_jobs_found() -> None:
    """The jobs that publish images are found as credential jobs."""
    found = {(name, job_id) for name, job_id, _ in _credential_jobs()}
    assert {
        ("ci.yml", "push"),
        ("ci.yml", "publish"),
        ("release.yml", "push"),
        ("release.yml", "publish"),
    } <= found


@pytest.mark.parametrize(
    ("command", "found"),
    [
        pytest.param("pip install foo", True, id="pip"),
        pytest.param("pip3.13 install foo", True, id="versioned"),
        pytest.param('"${VENV}/bin/pip" install foo', True, id="venv"),
        pytest.param("python -m pip wheel .", True, id="wheel"),
        pytest.param("pip download foo", True, id="download"),
        pytest.param("pip -q install foo", True, id="global-option"),
        pytest.param("pip --log pip.log install foo", True, id="option-value"),
        pytest.param(
            "python -m pip --disable-pip-version-check install foo",
            True,
            id="global-options",
        ),
        pytest.param("pip list", False, id="list"),
        pytest.param("echo pipeline install", False, id="other-word"),
    ],
)
def test_pip_pattern(command: str, found: bool) -> None:
    """The pattern finds pip commands that install or build packages.

    Args:
        command: A command.
        found: Whether the pattern finds it.
    """
    assert bool(PIP.search(command)) == found


@pytest.mark.parametrize(
    "command",
    [
        "pipx run foo",
        "pipenv install",
        "uv sync",
        "uv build",
        "python -m build",
        "poetry install",
    ],
)
def test_installers_pattern(command: str) -> None:
    """The pattern finds other installers and builds.

    Args:
        command: A command.
    """
    assert INSTALLERS.search(command)


def test_commands_split() -> None:
    """Commands chained on one line are checked one by one."""
    step = {"run": "pip install -r r.txt && pip install foo; echo a | tee b\n"}
    assert _commands(step) == [
        "pip install -r r.txt",
        "pip install foo",
        "echo a",
        "tee b",
    ]


def test_credential_jobs_install_hash_checked() -> None:
    """Jobs with publishing credentials install Python packages hash-checked.

    A compromised release of a package installed unpinned would run code in
    a job that holds registry credentials. So every pip command there that
    installs or builds packages is a `pip install` of exported locked
    requirements (`-r`) with hash checks, as wheels only and without
    resolving anything else, and no other installer is used. Actions
    (`uses:`) are pinned by commit and not checked here.
    """
    for name, job_id, job in _credential_jobs():
        for step in job["steps"]:
            for command in _commands(step):
                where = (name, job_id, step.get("name"), command)
                assert not INSTALLERS.search(command), where
                if match := PIP.search(command):
                    assert match.group(2) == "install", where
                    # Exactly these options and one requirements file (no
                    # other files, packages or options)
                    options = shlex.split(command[match.end() :])
                    assert options.count("-r") == 1, where
                    file = options.index("-r")
                    assert options[file + 1].startswith("$"), where
                    rest = options[:file] + options[file + 2 :]
                    assert rest == HASH_CHECKED, where


def _derive_version() -> dict[str, Any]:
    """Get the step of the `push` job of `ci.yml` that derives the version.

    Returns:
        The step.
    """
    jobs = _load(WORKFLOWS / "ci.yml")["jobs"]
    steps = {step["name"]: step for step in jobs["push"]["steps"]}
    names = list(steps)
    # Before the job logs in to Docker Hub
    assert names.index("Derive version") < names.index("Log in to Docker Hub")
    return steps["Derive version"]


def test_dev_version_from_locked_build_requirements() -> None:
    """The `push` job of `ci.yml` derives the version with the locked tools.

    setuptools-scm and its dependencies are the `build-system` group of
    `uv.lock`, exported by the uv image of the `Dockerfile` (pinned by
    digest), as the image build installs them, and run from the virtual
    environment they are installed into (with hash checks, see
    `test_credential_jobs_install_hash_checked`).
    """
    script = " ".join(_commands(_derive_version()))
    assert '"${uv_image}" export --frozen --only-group build-system' in script
    assert "--no-emit-project" in script
    assert "--network none" in script
    # pip installs exactly the exported requirements
    assert '--project /src > "${requirements}"' in script
    assert '--no-deps -r "${requirements}"' in script
    assert re.search(r"build-env/bin/python\"? -m setuptools_scm", script)


def test_dev_version_uv_image() -> None:
    """The step finds the uv image of the `Dockerfile`, with its digest.

    The step's `sed` command runs against the `Dockerfile`, as in the step
    (GNU `sed`, as on the GitHub runners).
    """
    sed = shutil.which("sed")
    assert sed
    (command,) = [
        command
        for command in _commands(_derive_version())
        if command.startswith('uv_image="$(sed ')
    ]
    args = shlex.split(command.removeprefix('uv_image="$(').removesuffix(')"'))
    assert args[0] == "sed"
    assert args[-1] == "Dockerfile"
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [sed, *args[1:]],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^FROM (ghcr\.io/astral-sh/uv:\S+) AS uv$", dockerfile, re.M)
    assert match
    assert re.fullmatch(r"ghcr\.io/astral-sh/uv:[^@]+@sha256:[0-9a-f]{64}", match[1])
    assert result.stdout == match[1] + "\n"


# The jobs of `ci.yml` that install the app's dependencies, with their extra
LOCKED_JOBS = {"lint": "dev", "test": "dev", "chart": "dev", "docs": "docs"}


@pytest.mark.parametrize(("job_id", "extra"), LOCKED_JOBS.items())
def test_jobs_on_lock(job_id: str, extra: str) -> None:
    """The jobs that install the app's dependencies install the locked ones.

    They run on the versions of `uv.lock`, as the image does, so that pull
    requests that change only the lock (e.g., Dependabot's) are tested on
    it, with uv of a pinned `astral-sh/setup-uv`; no pip command installs
    anything.

    Args:
        job_id: The ID of the job.
        extra: The extra that the job installs.
    """
    job = _load(WORKFLOWS / "ci.yml")["jobs"][job_id]
    steps = {step["name"]: step for step in job["steps"]}
    uv = steps["Set up uv"]
    assert re.fullmatch(r"astral-sh/setup-uv@[0-9a-f]{40}", uv["uses"])
    assert _commands(steps["Install dependencies"]) == [
        f'uv sync --locked --extra {extra} --python "$(which python)"',
        'echo "${PWD}/.venv/bin" >> "$GITHUB_PATH"',
    ]
    names = list(steps)
    assert names.index("Set up Python") < names.index("Set up uv")
    assert names.index("Set up uv") < names.index("Install dependencies")
    for step in job["steps"]:
        for command in _commands(step):
            assert not PIP.search(command), (job_id, step["name"], command)


def test_uv_version_locked() -> None:
    """Every `astral-sh/setup-uv` step installs the uv locked in `uv.lock`.

    The action reads the version from the lock (`version-file`), where uv is
    a dev dependency; it fails if the lock has none. Dependabot bumps it
    with the other locked packages.
    """
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert [package for package in lock["package"] if package["name"] == "uv"]
    setups = [
        step
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job in _load(path)["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert len(setups) == len(LOCKED_JOBS)
    for step in setups:
        assert step["with"] == {"version-file": "uv.lock"}, step
