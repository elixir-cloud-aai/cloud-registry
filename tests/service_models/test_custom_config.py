"""Tests for custom config models."""

from pydantic import ValidationError
import pytest

from cloud_registry.service_models.custom_config import IdConfig


def test_id_config():
    """A literal character set is accepted."""
    assert IdConfig(charset="ABC123", length=6).charset == "ABC123"


@pytest.mark.parametrize(
    "charset", ["", "string.ascii_uppercase + string.digits", "string.digits"]
)
def test_id_config_invalid_charset(charset):
    """Empty character sets and expressions (no longer evaluated since FOCA
    0.14) are rejected when the config is loaded.
    """
    with pytest.raises(ValidationError):
        IdConfig(charset=charset, length=6)
