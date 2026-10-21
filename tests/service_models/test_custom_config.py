"""Tests for custom config models."""

from pydantic import ValidationError
import pytest

from cloud_registry.service_models.custom_config import IdConfig


def test_id_config():
    """A literal character set and a positive length are accepted."""
    config = IdConfig(charset="ABC123", length=6)
    assert (config.charset, config.length) == ("ABC123", 6)


@pytest.mark.parametrize(
    "charset",
    [
        "",
        "string.ascii_uppercase + string.digits",
        "string.digits",
        "ABC/123",
        ".",
        "..",
    ],
)
def test_id_config_invalid_charset(charset):
    """Empty character sets, expressions (no longer evaluated since FOCA
    0.14), and character sets giving identifiers that aren't URL path
    segments are rejected when the config is loaded.
    """
    with pytest.raises(ValidationError):
        IdConfig(charset=charset, length=6)


@pytest.mark.parametrize("length", [0, -3])
def test_id_config_invalid_length(length):
    """Lengths that identifiers cannot be generated with are rejected."""
    with pytest.raises(ValidationError):
        IdConfig(charset="ABC123", length=length)
