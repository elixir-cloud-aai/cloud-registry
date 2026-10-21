"""Unit tests for `cloud_registry.utils`."""

from foca.config.access import get_app_config
import pytest

from cloud_registry.utils import (
    escape_log_value,
    get_collection,
    get_custom_config,
    get_public_url,
)


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
    of the app and the path, percent-encoded, and no trailing slash, except
    for the root of the host (empty path).
    """
    get_app_config(unit_app).model_extra["custom"].endpoints.service.api_path = api_path
    with unit_app.test_request_context(base_url=f"http://localhost{root}"):
        assert get_public_url(path) == url


@pytest.mark.parametrize(
    "host, url",
    [
        ("::1", "http://[::1]:80/registry/services"),
        ("[::1]", "http://[::1]:80/registry/services"),
        ("fe80::1%eth0", "http://[fe80::1%25eth0]:80/registry/services"),
        ("[fe80::1%eth0]", "http://[fe80::1%25eth0]:80/registry/services"),
        ("[fe80::1%25eth0]", "http://[fe80::1%25eth0]:80/registry/services"),
        ("example.org", "http://example.org:80/registry/services"),
        ("1.2.3.4", "http://1.2.3.4:80/registry/services"),
        # misconfigured with a port: not an IPv6 address, kept
        ("example.org:8443", "http://example.org:8443:80/registry/services"),
    ],
)
def test_get_public_url_host(unit_app, host, url):
    """IPv6 addresses are bracketed in public URLs (with encoded zone IDs),
    unless configured so.
    """
    get_app_config(unit_app).model_extra[
        "custom"
    ].endpoints.service.external_host = host
    with unit_app.test_request_context():
        assert get_public_url("/services") == url


@pytest.mark.parametrize(
    ("value", "escaped"),
    [
        ("MYSERV", "MYSERV"),
        ("a\nb\r", "a\\nb\\r"),
        ("a\x1b[31mb", "a\\x1b[31mb"),
        ("\u202e", "\\u202e"),
        ("\ud800", "\\ud800"),
        ("Zoë", "Zoë"),
        (None, "None"),
    ],
    ids=["plain", "line_breaks", "escape", "bidi", "surrogate", "unicode", "none"],
)
def test_escape_log_value(value, escaped):
    """Non-printable characters are escaped, others are kept."""
    assert escape_log_value(value) == escaped
