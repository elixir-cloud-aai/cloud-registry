"""Test for endpoint controller utility functions."""

from cloud_registry.utils import generate_id
from tests.mock_data import MOCK_ID_ONE_CHAR


def test_generate_id():
    """Test for generating random ID with literal character set."""
    assert generate_id(charset=MOCK_ID_ONE_CHAR, length=6) == "AAAAAA"
