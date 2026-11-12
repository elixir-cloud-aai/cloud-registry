"""Tests of the smoke test of verified TLS fetches (`scripts/smoke_tls.py`).

CI's smoke test runs the script in the app's image; here it runs with the
TLS settings of the host, and with settings that it must reject.
"""

import importlib.util
from pathlib import Path
import ssl
import subprocess
import sys

import pytest

from cloud_registry import safe_fetch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/smoke_tls.py"

# `scripts/` is not a package, so the script is loaded from its file
_spec = importlib.util.spec_from_file_location("smoke_tls", SCRIPT)
assert _spec and _spec.loader
smoke_tls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke_tls)


@pytest.fixture
def no_system_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No CA certificates of the system, as in an image without them.

    Returns:
        Directory where OpenSSL looks up CA certificates, empty.
    """
    directory = tmp_path / "certs"
    directory.mkdir()
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(directory))
    return directory


def test_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """The checks pass with the TLS settings of fetches."""
    assert smoke_tls.main() == 0
    assert "verified TLS fetch" in capsys.readouterr().out


def test_passes_from_stdin() -> None:
    """The checks pass when the script is read from standard input, as in CI.

    Unlike `test_passes`, this runs it as CI does: as `__main__`, without its
    file, with the exit status of `main()`.
    """
    result = subprocess.run(
        [sys.executable, "-"],
        input=SCRIPT.read_text(),
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_no_ca_certificates(
    no_system_cas: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settings without CA certificates fail, before any fetch."""
    # neither OpenSSL's lookup names nor CRLs count
    (no_system_cas / "ca-certificates.crt").touch()
    (no_system_cas / "0123abcd.r0").touch()
    # nor files that aren't certificates, or links to missing ones
    (no_system_cas / "89abcdef.0").touch()
    (no_system_cas / "4567cdef.0").symlink_to(no_system_cas / "missing.crt")
    assert smoke_tls.main() == 1
    assert "no CA certificates" in capsys.readouterr().err


def test_ca_directory(no_system_cas: Path) -> None:
    """CA certificates that OpenSSL looks up in a directory count."""
    ca, _ = smoke_tls._certificates()
    (no_system_cas / "0123abcd.0").write_bytes(ca)
    assert smoke_tls.main() == 0


def test_unverified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settings that don't check certificates fail."""

    def context() -> ssl.SSLContext:
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        return unverified

    monkeypatch.setattr(smoke_tls, "_default_tls_context", context)
    assert smoke_tls.main() == 1
    assert "untrusted certificate accepted" in capsys.readouterr().err


def test_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verified fetch that fails is reported."""

    def safe_get(*args: object, **kwargs: object) -> bytes:
        raise safe_fetch.Unreachable

    monkeypatch.setattr(smoke_tls, "safe_get", safe_get)
    assert smoke_tls.main() == 1
    assert "verified TLS fetch failed" in capsys.readouterr().err


def test_wrong_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verified fetch with another response body is reported."""

    def safe_get(*args: object, **kwargs: object) -> bytes:
        return b"{}"

    monkeypatch.setattr(smoke_tls, "safe_get", safe_get)
    assert smoke_tls.main() == 1
    assert "unexpected response" in capsys.readouterr().err
