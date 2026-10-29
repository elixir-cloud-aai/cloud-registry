"""Tests of the image build's downloads (`Dockerfile`).

The image build downloads packages only with hash checks: the runtime
dependencies and the build requirements of the app (`build-system.requires`)
are both exported from the lock (`uv.lock`), with their hashes.
"""

import os
from pathlib import Path
import re
import shlex
import subprocess
import tomllib

from uv import find_uv_bin

ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "Dockerfile"
# Dependency group that locks the build requirements of the app
GROUP = "build-system"


def _export(*args: str) -> str:
    """Export locked requirements with uv, as the image build does.

    Args:
        *args: Arguments that select the requirements (e.g., `--no-dev`).

    Returns:
        The exported requirements file.
    """
    # uv comes with the dev dependencies
    result = subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [
            find_uv_bin(),
            "export",
            "--frozen",
            "--no-emit-project",
            "--no-header",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        env={**os.environ, "UV_NO_CACHE": "1", "UV_OFFLINE": "1"},
    )
    return result.stdout


def _pinned(requirements: str) -> dict[str, list[str]]:
    """Get the pinned packages of a requirements file, with their hashes.

    Args:
        requirements: Requirements file, as `uv export` writes it.

    Returns:
        Hashes by package name.
    """
    lines = re.sub(r"\\\n\s*", " ", requirements).splitlines()
    packages = {}
    for line in lines:
        if not line or line.lstrip().startswith("#"):
            continue
        name, _, rest = line.partition("==")
        packages[name.strip()] = re.findall(r"--hash=sha256:([0-9a-f]{64})", rest)
    return packages


def _runs() -> list[tuple[list[str], list[str]]]:
    """Get the `RUN` instructions of the Dockerfile.

    Continued lines are joined, and comments are left out.

    Returns:
        The options of each instruction (e.g., `--network=none`), and its
        shell commands, one per command of a `&&` list.
    """
    lines = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    text = "\n".join(lines).replace("\\\n", " ")
    runs = []
    for run in re.findall(r"^RUN\s+(.+)$", text, flags=re.MULTILINE):
        match = re.fullmatch(r"((?:--\S+\s+)*)(.*)", run)
        assert match, run
        options, commands = match.groups()
        runs.append(
            (options.split(), [command.strip() for command in commands.split("&&")])
        )
    return runs


def _run_commands() -> list[str]:
    """Get the shell commands of the `RUN` instructions of the Dockerfile.

    Returns:
        Commands, one per command of a `&&` list.
    """
    return [command for _, commands in _runs() for command in commands]


def test_build_group_matches_build_system() -> None:
    """The dependency group lists exactly the build requirements of the app."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert (
        pyproject["dependency-groups"][GROUP] == (pyproject["build-system"]["requires"])
    )


def test_build_requirements_hashed() -> None:
    """The locked build requirements are complete, each with hashes."""
    packages = _pinned(_export("--only-group", GROUP))
    assert {"setuptools", "setuptools-scm"} <= packages.keys()
    for name, hashes in packages.items():
        assert hashes, name


def test_runtime_requirements_without_build_requirements() -> None:
    """The runtime requirements of the image leave out the build group."""
    packages = _pinned(_export("--no-dev"))
    assert "connexion" in packages
    assert "setuptools" not in packages
    assert "setuptools-scm" not in packages


def test_downloads_hash_checked() -> None:
    """Each pip command of the image build checks hashes or downloads nothing.

    pip commands that may download packages (`install`, `wheel`, `download`)
    take hash-pinned requirements only (`--require-hashes`), as wheels
    (`--only-binary :all:`), or no index at all (`--no-index`). Builds of the
    app use the hash-checked build requirements, not an isolated build
    environment, which pip would fill with unchecked downloads.
    """
    commands = []
    for args in map(shlex.split, _run_commands()):
        # `pip`, `<venv>/bin/pip` or `python -m pip`
        starts = [i for i, arg in enumerate(args) if Path(arg).name == "pip"]
        if starts:
            commands.append(args[starts[0] + 1 :])
        # uv only exports the requirements
        if "uv" in args:
            assert args[args.index("uv") + 1] == "export", args
    subcommands = [args[0] for args in commands]
    assert set(subcommands) <= {"install", "wheel"}, subcommands
    # One build of the app (its wheel)
    assert subcommands.count("wheel") == 1, subcommands
    for args in commands:
        if "--no-index" not in args:
            assert "--require-hashes" in args, args
            assert args[args.index("--only-binary") + 1] == ":all:", args
        if args[0] == "wheel":
            assert "--no-index" in args, args
            assert "--no-build-isolation" in args, args
            assert "--check-build-dependencies" in args, args
        elif "--no-index" in args:
            # Wheels only, no builds
            targets = [arg for arg in args[1:] if not arg.startswith("-")]
            assert targets, args
            assert all(target.endswith(".whl") for target in targets), args


def test_builds_offline() -> None:
    """The steps that build and install the app have no network.

    So that a download (e.g., of a build requirement missing from the lock)
    fails the image build, instead of only being left out by `--no-index`.
    """
    offline = [
        options
        for options, commands in _runs()
        if any(re.search(r"\bpip\s.*--no-index\b", command) for command in commands)
    ]
    assert len(offline) == 2, offline
    for options in offline:
        assert "--network=none" in options, options


def test_uv_mounted_only() -> None:
    """The uv image is only mounted into the steps that use it, not copied."""
    text = DOCKERFILE.read_text()
    assert not re.search(r"^COPY\s+--from=uv\b", text, flags=re.MULTILINE)
    for command in _run_commands():
        if re.search(r"\buv\s+export\b", command):
            assert "UV_NO_CACHE=1" in command, command
