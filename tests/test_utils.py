"""Unit tests for `cloud_registry.utils`."""

from foca.config.access import get_app_config
import pytest

from cloud_registry.utils import get_collection, get_custom_config


def test_get_custom_config(unit_app):
    """The custom config of the current app is returned."""
    with unit_app.app_context():
        config = get_custom_config()
    assert config is get_app_config(unit_app).model_extra["custom"]


def test_get_custom_config_missing(unit_app):
    """A missing custom config raises an error."""
    del get_app_config(unit_app).model_extra["custom"]
    with unit_app.app_context(), pytest.raises(RuntimeError):
        get_custom_config()


def test_get_collection(unit_app, services):
    """Configured collections of the service store are returned."""
    with unit_app.app_context():
        assert get_collection("services") is services
        with pytest.raises(KeyError):
            get_collection("unknown")
