"""Tests of the release state and asset check (`scripts/release_assets.py`)."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_assets.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("release_assets", SCRIPT)
assert _spec and _spec.loader
release_assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_assets)

TAG = "v1.2.3"


def _asset(
    path: Path, digest: str | None = None, state: str = "uploaded"
) -> dict[str, Any]:
    """Return an asset of a file as the GitHub API returns it.

    Args:
        path: Local file of the asset.
        digest: Digest of the asset; that of the file if `None`.
        state: State of the upload.

    Returns:
        The asset.
    """
    if digest is None:
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return {"name": path.name, "digest": digest, "state": state}


def _release(
    tag: str = TAG,
    draft: bool = False,
    assets: list[dict[str, Any]] | None = None,
    immutable: bool = True,
) -> dict[str, Any]:
    """Return a release as the GitHub API returns it.

    Args:
        tag: Tag of the release.
        draft: Whether it is a draft.
        assets: Its assets.
        immutable: Whether it is immutable.

    Returns:
        The release.
    """
    return {
        "tag_name": tag,
        "draft": draft,
        "immutable": immutable and not draft,
        "assets": assets or [],
    }


@pytest.fixture
def files(tmp_path: Path) -> list[Path]:
    """Return the local assets of a release.

    Args:
        tmp_path: Temporary directory.

    Returns:
        A chart and its Sigstore bundle.
    """
    chart = tmp_path / "chart-1.2.3.tgz"
    chart.write_bytes(b"chart")
    bundle = tmp_path / "chart-1.2.3.tgz.sigstore.json"
    bundle.write_text("{}")
    return [chart, bundle]


def test_read_releases_pages() -> None:
    """Pages of releases (`--slurp`) and flat lists give the releases."""
    pages = json.dumps([[{"tag_name": "v1"}, {"tag_name": "v2"}], [{"tag_name": "v3"}]])
    assert [r["tag_name"] for r in release_assets.read_releases(pages)] == [
        "v1",
        "v2",
        "v3",
    ]
    assert release_assets.read_releases('[{"tag_name": "v1"}]') == [{"tag_name": "v1"}]
    assert release_assets.read_releases("[]") == []
    assert release_assets.read_releases("[[]]") == []


@pytest.mark.parametrize("data", ["{}", '"x"', "[1]", "[[1]]", "not json"])
def test_read_releases_invalid(data: str) -> None:
    """Anything but (pages of) release objects is rejected."""
    with pytest.raises(ValueError):
        release_assets.read_releases(data)


def test_state_none(files: list[Path]) -> None:
    """Without a release of the tag, one is to be created."""
    releases = [_release("v1.2.2"), _release("v1.2.4", draft=True)]
    assert release_assets.release_state(TAG, releases, files) == ("none", [], [])


def test_state_draft(files: list[Path]) -> None:
    """A single draft (with any assets) is to be finished."""
    releases = [_release("v1.2.2"), _release(draft=True, assets=[_asset(files[0])])]
    assert release_assets.release_state(TAG, releases, files) == ("draft", [], [])


def test_state_several_drafts(files: list[Path]) -> None:
    """Several drafts of the tag are an error (which one to publish?)."""
    releases = [_release(draft=True), _release(draft=True)]
    state, errors, warnings = release_assets.release_state(TAG, releases, files)
    assert state is None
    assert len(errors) == 1 and "2 draft releases" in errors[0]
    assert warnings == []


def test_state_published_complete(files: list[Path]) -> None:
    """A published release with all assets needs no upload."""
    releases = [_release(assets=[_asset(path) for path in files])]
    assert release_assets.release_state(TAG, releases, files) == (
        "published",
        [],
        [],
    )


def test_state_published_without_digests(files: list[Path]) -> None:
    """Assets without a digest (older uploads) count by name."""
    assets = [
        {"name": path.name, "digest": None, "state": "uploaded"} for path in files
    ]
    assert release_assets.release_state(TAG, [_release(assets=assets)], files)[0] == (
        "published"
    )


def test_state_published_changed(files: list[Path]) -> None:
    """Assets with another digest are kept, with a warning."""
    assets = [_asset(files[0]), _asset(files[1], digest="sha256:" + "0" * 64)]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets)], files
    )
    assert (state, errors) == ("published", [])
    assert len(warnings) == 1
    assert warnings[0].endswith(
        f"other versions of {files[1].name} (of an earlier attempt or uploaded"
        " by hand); they are kept, as published assets can't be replaced"
    )


@pytest.mark.parametrize(
    ("immutable", "remedy"),
    [(True, "release a new patch version"), (False, "gh release upload v1.2.3")],
)
def test_state_published_missing(
    files: list[Path], immutable: bool, remedy: str
) -> None:
    """A published release without all assets is an error, with the remedy."""
    # An upload that did not finish does not count
    assets = [_asset(files[0], state="starter")]
    state, errors, warnings = release_assets.release_state(
        TAG, [_release(assets=assets, immutable=immutable)], files
    )
    assert state is None
    assert len(errors) == 1
    assert files[0].name in errors[0] and files[1].name in errors[0]
    assert remedy in errors[0]
    assert warnings == []


def test_state_published_and_drafts(files: list[Path]) -> None:
    """A published release wins over drafts of the tag, which are reported."""
    releases = [
        _release(draft=True),
        _release(assets=[_asset(path) for path in files]),
        _release(draft=True),
    ]
    state, errors, warnings = release_assets.release_state(TAG, releases, files)
    assert (state, errors) == ("published", [])
    assert len(warnings) == 1 and "2 draft release(s)" in warnings[0]


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script.

    Args:
        tmp_path: Working directory.
        *args: Arguments of the script.

    Returns:
        The completed process.
    """
    return subprocess.run(  # noqa: S603 (fixed arguments, no shell)
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


def test_main(tmp_path: Path, files: list[Path]) -> None:
    """The command prints the state as a `GITHUB_OUTPUT` line."""
    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps([[_release(draft=True)]]))
    result = _run(tmp_path, "state", TAG, "--releases", str(releases), *map(str, files))
    assert (result.returncode, result.stdout, result.stderr) == (0, "state=draft\n", "")


def test_main_annotations(tmp_path: Path, files: list[Path]) -> None:
    """Errors and warnings are printed as annotations, and errors fail."""
    releases = tmp_path / "releases.json"
    assets = [_asset(files[0], digest="sha256:" + "0" * 64)]
    releases.write_text(json.dumps([[_release(assets=assets)]]))
    result = _run(tmp_path, "state", TAG, "--releases", str(releases), *map(str, files))
    assert result.returncode == 1
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert [line.split("::")[1] for line in lines] == ["warning", "error"]


def test_main_invalid_releases(tmp_path: Path, files: list[Path]) -> None:
    """Releases that can't be read or parsed fail with an annotation."""
    missing = tmp_path / "missing.json"
    result = _run(tmp_path, "state", TAG, "--releases", str(missing), str(files[0]))
    assert result.returncode == 1
    assert result.stderr.startswith(f"::error::Cannot check release {TAG}: ")
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}")
    result = _run(tmp_path, "state", TAG, "--releases", str(invalid), str(files[0]))
    assert result.returncode == 1
    assert "Releases are not a JSON list" in result.stderr


def test_main_in_process(
    tmp_path: Path, files: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`main` works in process too (for coverage), with explicit arguments."""
    releases = tmp_path / "releases.json"
    releases.write_text("[]")
    argv = ["state", TAG, "--releases", str(releases), *map(str, files)]
    assert release_assets.main(argv) == 0
    assert capsys.readouterr().out == "state=none\n"
    releases.write_text(json.dumps([_release(draft=True), _release(draft=True)]))
    assert release_assets.main(argv) == 1
    assert capsys.readouterr().err.startswith("::error::2 draft releases")
    assets = [_asset(path, digest="sha256:" + "0" * 64) for path in files]
    releases.write_text(json.dumps([_release(assets=assets)]))
    assert release_assets.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "state=published\n"
    assert captured.err.startswith("::warning::")
    releases.write_text("{}")
    assert release_assets.main(argv) == 1
