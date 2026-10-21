"""Unit tests for `cloud_registry.utils`."""

from typing import Any

from flask import Flask
from foca.config.access import get_app_config
from pymongo.collection import Collection
import pytest

from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import (
    get_collection,
    get_custom_config,
    get_public_url,
    is_date_time,
)


def _extra(app: Flask) -> dict[str, Any]:
    """Get the extra fields of the config of an app, e.g., the custom config.

    Args:
        app: Flask app with FOCA's config.

    Returns:
        Extra fields of the config.
    """
    extra = get_app_config(app).model_extra
    assert extra is not None
    return extra


def _custom_config(app: Flask) -> CustomConfig:
    """Get the custom config of an app.

    Args:
        app: Flask app with FOCA's config.

    Returns:
        Custom config.
    """
    with app.app_context():
        return get_custom_config()


def test_get_custom_config(unit_app: Flask) -> None:
    """The custom config of the current app is returned."""
    with unit_app.app_context():
        config = get_custom_config()
    assert config is _extra(unit_app)["custom"]


def test_get_custom_config_missing(unit_app: Flask) -> None:
    """A missing custom config raises an error."""
    del _extra(unit_app)["custom"]
    with unit_app.app_context(), pytest.raises(RuntimeError):
        get_custom_config()


def test_get_collection(unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
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
def test_get_public_url(
    unit_app: Flask, api_path: str, root: str, path: str, url: str
) -> None:
    """Public URLs have the configured path prefix in front of the path.

    The prefix is followed by the root path of the app and the path,
    percent-encoded, with no trailing slash, except for the root of the host
    (empty path).
    """
    _custom_config(unit_app).endpoints.service.api_path = api_path
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
def test_get_public_url_host(unit_app: Flask, host: str, url: str) -> None:
    """IPv6 addresses are bracketed in public URLs, with encoded zone IDs.

    Unless they are configured so.
    """
    _custom_config(unit_app).endpoints.service.external_host = host
    with unit_app.test_request_context():
        assert get_public_url("/services") == url


@pytest.mark.parametrize(
    "value",
    [
        "2020-11-08T10:20:30Z",
        "2020-11-08t10:20:30z",
        "2020-02-29T10:20:30.1234567-05:30",
        "2020-11-08T10:20:30+23:59",
        "2016-12-31T23:59:60Z",
        1,
        None,
    ],
    ids=[
        "utc",
        "lowercase",
        "fraction_offset",
        "max_offset",
        "leap_second",
        "int",
        "none",
    ],
)
def test_is_date_time(value: object) -> None:
    """RFC 3339 date-times are valid; non-strings are left to type checks."""
    assert is_date_time(value)


@pytest.mark.parametrize(
    "value",
    [
        "2021-02-29T10:20:30Z",
        "2020-11-31T10:20:30Z",
        "2020-11-08T24:00:00Z",
        "2020-11-08T10:60:00Z",
        "2020-11-08T10:20:61Z",
        "2020-11-08T10:20:30+24:00",
        "2020-11-08T10:20:30+01:60",
        "2020-11-08T10:20:30",
        "2020-11-08 10:20:30Z",
        "2020-11-08T10:20:30+0100",
        # fullwidth digits (non-ASCII digits that `\d` would match)
        "\uff12\uff10\uff12\uff10-11-08T10:20:30Z",
        "",
    ],
    ids=[
        "no_leap_year",
        "invalid_day",
        "hour",
        "minute",
        "second",
        "offset_hour",
        "offset_minute",
        "no_offset",
        "space",
        "offset_no_colon",
        "non_ascii_digits",
        "empty",
    ],
)
def test_is_date_time_invalid(value: object) -> None:
    """Other strings are invalid."""
    assert not is_date_time(value)
