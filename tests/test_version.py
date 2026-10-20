"""Tests for the package version, derived from Git tags."""

from importlib.metadata import version
from pathlib import Path
import re
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # pytest depends on tomli on Python 3.10
    import tomli as tomllib

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"
# Canonical PEP 440 version (PEP 440, appendix B), with an optional local
# version label (e.g., `+g<commit>` for untagged commits)
PEP_440 = re.compile(
    r"([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*"
    r"((a|b|rc)(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?"
    r"(\+[a-z0-9]+(\.[a-z0-9]+)*)?"
)


def test_installed_version():
    """The installed package has a valid PEP 440 version."""
    assert PEP_440.fullmatch(version("cloud-registry"))


def test_fallback_version():
    """Builds without Git metadata get a valid development version."""
    config = tomllib.loads(PYPROJECT.read_text())
    fallback = config["tool"]["setuptools_scm"]["fallback_version"]
    assert PEP_440.fullmatch(fallback)
    assert ".dev" in fallback
