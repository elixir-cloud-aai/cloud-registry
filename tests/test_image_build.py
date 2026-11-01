"""Tests of the image build (`Dockerfile`, `.dockerignore`).

The image build downloads packages only with hash checks: the runtime
dependencies and the build requirements of the app (`build-system.requires`)
are both exported from the lock (`uv.lock`), with their hashes. Local
secrets (`.env` files) are kept out of its build context.
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
DOCKERIGNORE = ROOT / ".dockerignore"
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
        shell commands, one per command of a list or pipeline (separated by
        `&&`, `||`, `;` or `|`).
    """
    lines = [
        line
        for line in DOCKERFILE.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    text = "\n".join(lines).replace("\\\n", " ")
    runs = []
    for run in re.findall(r"^RUN\s+(.+)$", text, flags=re.MULTILINE):
        # Heredocs (`RUN <<EOF`) would hide their commands from the checks
        assert "<<" not in run, run
        match = re.fullmatch(r"((?:--\S+\s+)*)(.*)", run)
        assert match, run
        options, commands = match.groups()
        runs.append(
            (options.split(), [c.strip() for c in re.split(r"&&|\|\||;|\|", commands)])
        )
    return runs


def _run_commands() -> list[str]:
    """Get the shell commands of the `RUN` instructions of the Dockerfile.

    Returns:
        Commands, one per command of a list or pipeline.
    """
    return [command for _, commands in _runs() for command in commands]


def _pip_args(command: str) -> list[str] | None:
    """Get the arguments of pip in a shell command.

    Args:
        command: Shell command (e.g., of a `RUN` instruction).

    Returns:
        The arguments after `pip` (`pip`, `pip3`, `pip3.13`, `<venv>/bin/pip`
        or `python -m pip`), starting with the pip command (e.g., `install`),
        or `None` if the command doesn't run pip.
    """
    args = shlex.split(command)
    for i, arg in enumerate(args):
        if re.fullmatch(r"pip(3(\.\d+)?)?", Path(arg).name):
            return args[i + 1 :]
    return None


def test_build_group_matches_build_system() -> None:
    """The dependency group lists exactly the build requirements of the app."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    group = pyproject["dependency-groups"][GROUP]
    requires = pyproject["build-system"]["requires"]
    assert group == requires


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
    for command in _run_commands():
        pip_args = _pip_args(command)
        if pip_args is not None:
            commands.append(pip_args)
        # uv only exports the requirements
        args = shlex.split(command)
        if "uv" in args:
            assert args[args.index("uv") + 1] == "export", args
    subcommands = [args[0] for args in commands]
    assert set(subcommands) <= {"install", "wheel"}, subcommands
    # The app is built as a wheel (checked below), not by `pip install` of
    # its sources
    assert "wheel" in subcommands, subcommands
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
        if any("--no-index" in (_pip_args(command) or []) for command in commands)
    ]
    # At least the build and the install of the wheel
    assert offline
    for options in offline:
        assert "--network=none" in options, options


def test_uv_mounted_only() -> None:
    """The uv image is only mounted into the steps that use it, not copied.

    The steps also keep no uv cache (`UV_NO_CACHE=1`) in their layers.
    """
    text = DOCKERFILE.read_text()
    assert not re.search(r"^COPY\s+--from=uv\b", text, flags=re.MULTILINE)
    for command in _run_commands():
        if re.search(r"\buv\s+export\b", command):
            assert "UV_NO_CACHE=1" in command, command


def _pattern(pattern: str) -> re.Pattern[str]:
    """Compile a `.dockerignore` pattern to a regular expression.

    As Docker matches them: `**` matches any number of directories (also
    none), `*` and `?` match within one path segment, and a pattern that
    matches a directory also matches everything in it, and character
    classes (`[...]`) match one character of a path segment.

    Args:
        pattern: Pattern, without a leading `!`.

    Returns:
        Regular expression that matches the paths (relative to the build
        context) that the pattern matches.
    """
    regex = ""
    rest = pattern.strip("/")
    while rest:
        if rest.startswith("**/"):
            regex += "(?:.*/)?"
            rest = rest[3:]
        elif rest.startswith("**"):
            regex += ".*"
            rest = rest[2:]
        elif rest[0] == "*":
            regex += "[^/]*"
            rest = rest[1:]
        elif rest[0] == "?":
            regex += "[^/]"
            rest = rest[1:]
        elif rest[0] == "[":
            end = rest.index("]")
            regex += f"[{re.escape(rest[1:end])}]"
            rest = rest[end + 1 :]
        else:
            regex += re.escape(rest[0])
            rest = rest[1:]
    return re.compile(f"{regex}(?:/.*)?")


def _in_context(path: str) -> bool:
    """Check whether `.dockerignore` keeps a path in the build context.

    The last pattern that matches the path decides, as in Docker.

    Args:
        path: Path, relative to the build context.

    Returns:
        Whether the path is sent to the build.
    """
    included = True
    for line in DOCKERIGNORE.read_text().splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        if _pattern(pattern.removeprefix("!")).fullmatch(path):
            included = negated
    return included


def test_context_files() -> None:
    """The build context has the files the image build needs, not others."""
    for path in [
        "cloud_registry/app.py",
        "cloud_registry/api/additions.openapi.yaml",
        "pyproject.toml",
        "uv.lock",
        "scripts/image_requirements.py",
    ]:
        assert _in_context(path), path
    for path in [
        "tests/test_app.py",
        "docs/index.md",
        "cloud_registry/__pycache__/app.cpython-313.pyc",
        "cloud_registry/app.pyc",
        "cloud_registry/ga4gh/registry/__pycache__/service.cpython-313.pyc",
    ]:
        assert not _in_context(path), path


def test_context_without_env_files() -> None:
    """Local `.env` files are not sent to the build.

    Also not in the included paths (e.g., `cloud_registry/`), whose files
    the build copies into a layer of its build stage; nor files whose names
    start with `.env` (e.g., `.envrc` of direnv), or directories named so.
    Files that only contain `.env` in their names are kept.
    """
    for path in [
        ".env",
        ".env.local",
        ".env.example",
        "cloud_registry/.env",
        "cloud_registry/.env.production",
        "cloud_registry/.env-prod",
        "cloud_registry/.envrc",
        "cloud_registry/.env/settings",
        "cloud_registry/ga4gh/registry/.env",
        "scripts/.env",
    ]:
        assert not _in_context(path), path
    assert _in_context("cloud_registry/api/dot.env.yaml")
