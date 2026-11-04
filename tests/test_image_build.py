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

from packaging.requirements import Requirement
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


def _locked_versions(requirements: str) -> dict[str, str]:
    """Get the locked versions of a requirements file.

    Args:
        requirements: Requirements file, as `uv export` writes it.

    Returns:
        Versions by package name, of the requirements pinned with `==`.
    """
    return dict(
        re.findall(r"^([A-Za-z0-9._-]+)==([^\s;\\]+)", requirements, flags=re.MULTILINE)
    )


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


def _uv_args(command: str) -> list[str] | None:
    """Get the arguments of uv in a shell command.

    Args:
        command: Shell command (e.g., of a `RUN` instruction).

    Returns:
        The arguments after `uv`, starting with its command (e.g., `pip`), or
        `None` if the command doesn't run uv.
    """
    args = shlex.split(command)
    if args and Path(args[0]).name == "uv":
        return args[1:]
    return None


def _runs_pip(command: str) -> bool:
    """Tell whether a shell command runs pip itself (not `uv pip`).

    Args:
        command: Shell command (e.g., of a `RUN` instruction).

    Returns:
        Whether the command runs `pip`, `pip3`, `pip3.13`, `<venv>/bin/pip`
        or `python -m pip`.
    """
    args = shlex.split(command)
    if _uv_args(command) is not None:
        return False
    return any(re.fullmatch(r"pip(3(\.\d+)?)?", Path(arg).name) for arg in args)


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


def test_build_requirements_satisfied() -> None:
    """The locked build requirements satisfy `build-system.requires`.

    The image builds the app without build isolation, with the locked build
    requirements, which `uv build` doesn't check against
    `build-system.requires`.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    locked = _locked_versions(_export("--only-group", GROUP))
    for requirement in map(Requirement, pyproject["build-system"]["requires"]):
        assert requirement.specifier.contains(locked[requirement.name]), requirement


def test_locked_foca_is_base_image_foca() -> None:
    """The lock has the FOCA release of the base image.

    The image build fails otherwise (`scripts/image_requirements.py`); this
    test fails earlier, without building the image. A FOCA commit pin
    (temporary) has no locked release, and is checked by
    `scripts/image_requirements.py`.
    """
    locked = _locked_versions(_export("--no-dev"))
    match = re.search(
        r"^FROM elixircloud/foca:(?P<version>[^-@\s]+)-py",
        DOCKERFILE.read_text(),
        flags=re.MULTILINE,
    )
    assert match, "No FOCA base image in the Dockerfile"
    assert locked.get("foca", match["version"]) == match["version"]


def test_dockerfile_exports() -> None:
    """The image build exports the runtime and the build requirements only.

    So the other tests of the exports check what the `Dockerfile` selects
    (e.g., not `--all-groups`, which would add the dev dependencies).
    """
    selections = []
    for command in _run_commands():
        args = _uv_args(command)
        if args is None or args[0] != "export":
            continue
        options = args[1:]
        for option in ("--project", "--output-file"):
            del options[options.index(option) : options.index(option) + 2]
        assert {"--frozen", "--no-emit-project"} <= set(options), args
        selections.append(
            [arg for arg in options if arg not in {"--frozen", "--no-emit-project"}]
        )
    assert sorted(selections) == [["--no-dev"], ["--only-group", GROUP]]


def test_runtime_requirements_without_build_requirements() -> None:
    """The runtime requirements of the image leave out the build group."""
    packages = _pinned(_export("--no-dev"))
    assert "connexion" in packages
    assert "setuptools" not in packages
    assert "setuptools-scm" not in packages


def test_downloads_hash_checked() -> None:
    """Each install of the image build checks hashes or downloads nothing.

    uv installs and builds everything; pip isn't run. uv commands that may
    download packages (`uv pip install`) take hash-pinned requirements only
    (`--require-hashes`, `-r`), as wheels (`--only-binary :all:`), without
    resolving anything else (`--no-deps`), or no index at all (`--no-index`,
    `--offline`), and then wheels only. The app is built offline with the
    hash-checked build requirements, not in an isolated build environment,
    which would be filled with unchecked downloads.
    """
    commands = []
    for command in _run_commands():
        assert not _runs_pip(command), command
        uv_args = _uv_args(command)
        if uv_args is not None:
            commands.append(uv_args)
    subcommands = [args[0] for args in commands]
    assert set(subcommands) <= {"export", "venv", "pip", "build"}, subcommands
    # The app is built as a wheel (checked below), not by an install of its
    # sources
    assert subcommands.count("build") == 1, subcommands
    # As nothing is resolved (`--no-deps`), the installed packages are checked
    assert ["pip", "check", "--system"] in commands, commands
    for args in commands:
        if args[:2] == ["pip", "check"]:
            # Checks the installed packages, downloads nothing
            continue
        if args[0] == "pip":
            assert args[1] == "install", args
            if "--no-index" in args:
                assert "--offline" in args, args
                targets = [arg for arg in args[2:] if not arg.startswith("-")]
                assert targets, args
                assert all(target.endswith(".whl") for target in targets), args
            else:
                assert "--require-hashes" in args, args
                assert args[args.index("--only-binary") + 1] == ":all:", args
                assert "-r" in args, args
            assert "--no-deps" in args, args
            # The image has a read-only root file system, so Python can't
            # cache bytecode at runtime; the build environment needs none
            if "--system" in args:
                assert "--compile-bytecode" in args, args
        elif args[0] == "build":
            assert "--offline" in args, args
            assert "--no-build-isolation" in args, args
            assert "--wheel" in args, args
        elif args[0] == "export":
            assert "--frozen" in args, args
        elif args[0] == "venv":
            # `--seed` would download pip without hash checks
            assert "--seed" not in args, args


def test_builds_offline() -> None:
    """The steps that build and install the app have no network.

    So that a download (e.g., of a build requirement missing from the lock)
    fails the image build, instead of only being refused by `--offline`.
    """
    offline = [
        options
        for options, commands in _runs()
        if any("--offline" in (_uv_args(command) or []) for command in commands)
    ]
    # At least the build and the install of the wheel
    assert len(offline) >= 2, offline
    for options in offline:
        assert "--network=none" in options, options


def test_uv_mounted_only() -> None:
    """The uv image is only mounted into the steps that use it, not copied.

    The steps also keep no uv cache (`UV_NO_CACHE=1`) in their layers and
    download no Python (`UV_PYTHON_DOWNLOADS=never`): they use the one of
    the base image.
    """
    text = DOCKERFILE.read_text()
    assert not re.search(r"^COPY\s+--from=uv\b", text, flags=re.MULTILINE)
    uses = [
        (options, commands)
        for options, commands in _runs()
        if any(_uv_args(command) is not None for command in commands)
    ]
    assert uses
    for options, commands in uses:
        assert "--mount=from=uv,source=/uv,target=/usr/local/bin/uv" in options
        assert commands[0] == "export UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never"


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
            # as in Docker, copied unchanged (e.g., ranges)
            regex += rest[: end + 1]
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


def test_context_without_secret_files() -> None:
    """Other common secret files are not sent to the build."""
    for name in [
        "prod.env",
        "server.pem",
        "tls.key",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ecdsa",
        "keystore.p12",
        "keystore.pfx",
        ".git-credentials",
        ".pypirc",
        "credentials.json",
        "credentials-prod.json",
        ".netrc",
    ]:
        assert not _in_context(name), name
        assert not _in_context(f"cloud_registry/{name}"), name
        assert not _in_context(f"cloud_registry/ga4gh/{name}"), name
    for name in ["keys.py", "credentials.py", "tokens.pem.yaml"]:
        assert _in_context(f"cloud_registry/{name}"), name
