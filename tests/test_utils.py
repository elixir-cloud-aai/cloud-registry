"""Unit tests for `cloud_registry.utils`."""

from foca.config.access import get_app_config
import pytest

from cloud_registry.utils import get_collection, get_custom_config, get_public_url


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


@pytest.mark.parametrize(
    "api_path, root, path, url",
    [
        ("", "", "/services", "http://1.2.3.4:80/services"),
        ("registry", "", "/services", "http://1.2.3.4:80/registry/services"),
        ("/registry/", "/app", "/services/", "http://1.2.3.4:80/registry/app/services"),
        ("registry", "", "/services/a b", "http://1.2.3.4:80/registry/services/a%20b"),
        ("", "", "", "http://1.2.3.4:80/"),
    ],
)
def test_get_public_url(unit_app, api_path, root, path, url):
    """Public URLs have the configured path prefix in front of the root path
    of the app and the path, percent-encoded.
    """
    get_app_config(unit_app).model_extra["custom"].endpoints.service.api_path = api_path
    with unit_app.test_request_context(base_url=f"http://localhost{root}"):
        assert get_public_url(path) == url
