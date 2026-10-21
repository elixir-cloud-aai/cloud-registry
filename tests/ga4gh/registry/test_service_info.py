"""Tests for service info from the config."""

from copy import deepcopy
from importlib.metadata import PackageNotFoundError

import pytest

from cloud_registry.ga4gh.registry import service_info as module
from cloud_registry.ga4gh.registry.service_info import (
    InvalidServiceInfo,
    check_service_info,
    get_service_info,
    package_fields,
)
from tests.mock_data import SERVICE_INFO_CONFIG

REQUIRED = ("id", "name", "type", "organization", "version")


def _configure(unit_app, **fields) -> dict:
    """Set fields of the configured service info (`None` removes them)."""
    config = unit_app.config.foca.custom.endpoints.service_info
    for field, value in fields.items():
        if value is None:
            del config[field]
        else:
            config[field] = value
    return config


def _update(unit_app, **fields) -> None:
    """Update the configured service info, keeping `None` values (as config
    overlays do).
    """
    unit_app.config.foca.custom.endpoints.service_info.update(fields)


def _not_installed(name):
    raise PackageNotFoundError(name)


def test_package_fields(monkeypatch):
    """The version is taken from the package metadata, if installed."""
    monkeypatch.setattr(module, "version", lambda name: f"{name} 1.2.3")
    assert package_fields() == {"version": "cloud-registry 1.2.3"}
    monkeypatch.setattr(module, "version", _not_installed)
    assert package_fields() == {}


def test_get_service_info(unit_app):
    """The configured service info is returned."""
    with unit_app.app_context():
        assert get_service_info() == SERVICE_INFO_CONFIG


def test_get_service_info_runtime_fields(unit_app):
    """Runtime fields are added unless configured."""
    _configure(unit_app, version=None)
    with unit_app.app_context():
        info = get_service_info(
            runtime_fields=lambda: {"version": "runtime", "name": "runtime"}
        )
    assert info == {**SERVICE_INFO_CONFIG, "version": "runtime"}


def test_get_service_info_copy(unit_app):
    """Changing the returned service info doesn't change the config."""
    _update(unit_app, **{"x-links": [{"url": "https://example.org"}]})
    with unit_app.app_context():
        info = get_service_info()
        info["organization"]["name"] = "changed"
        info["x-links"].append({})
        assert get_service_info() == {
            **SERVICE_INFO_CONFIG,
            "x-links": [{"url": "https://example.org"}],
        }


def test_get_service_info_nulls(unit_app):
    """Fields and list items set to `null` are removed, also nested."""
    organization = {**SERVICE_INFO_CONFIG["organization"], "x-logo": None}
    links = [None, {"url": "https://example.org", "title": None}]
    _update(unit_app, contactUrl=None, organization=organization, **{"x-links": links})
    with unit_app.app_context():
        info = get_service_info()
        check_service_info()
    expected = {k: v for k, v in SERVICE_INFO_CONFIG.items() if k != "contactUrl"}
    assert info == {**expected, "x-links": [{"url": "https://example.org"}]}


def test_null_runtime_field(unit_app):
    """A required field set to `null` is missing, even if set at runtime."""
    _update(unit_app, version=None)
    with unit_app.app_context(), pytest.raises(InvalidServiceInfo, match="'version'"):
        check_service_info(runtime_fields=lambda: {"version": "1.2.3"})


def test_check_service_info(unit_app, caplog):
    """Valid service info passes and is logged."""
    caplog.set_level("INFO")
    with unit_app.app_context():
        check_service_info()
    assert "Serving service info 'org.ga4gh.myservice', version '1.0.0'" in caplog.text


def test_check_service_info_required_only(unit_app):
    """Service info with the required fields only is valid."""
    for field in set(SERVICE_INFO_CONFIG) - set(REQUIRED):
        _configure(unit_app, **{field: None})
    with unit_app.app_context():
        check_service_info()


def test_check_service_info_package_version(unit_app):
    """The package version can provide the required version."""
    _configure(unit_app, version=None)
    with unit_app.app_context():
        check_service_info(runtime_fields=lambda: {"version": "1.2.3"})


@pytest.mark.parametrize("field", REQUIRED)
def test_check_service_info_missing(unit_app, field):
    """Service info without a required field is invalid."""
    _configure(unit_app, **{field: None})
    with (
        unit_app.app_context(),
        pytest.raises(
            InvalidServiceInfo,
            match=rf"in `custom\.endpoints\.service_info`: '{field}' is a required",
        ),
    ):
        check_service_info(runtime_fields=dict)


@pytest.mark.parametrize(
    "fields, path",
    [
        ({"contactUrl": "support"}, "contactUrl"),
        ({"documentationUrl": "docs example org"}, "documentationUrl"),
        ({"organization": {"name": "org", "url": "/org"}}, "organization.url"),
        ({"organization": {"name": "org"}}, "organization"),
        ({"createdAt": "yesterday"}, "createdAt"),
        ({"updatedAt": "2020-11-08"}, "updatedAt"),
        ({"updatedAt": "2020-11-08T00:00:00"}, "updatedAt"),
        ({"updatedAt": "2020-13-45T25:99:99Z"}, "updatedAt"),
        ({"updatedAt": "2021-02-29T00:00:00Z"}, "updatedAt"),
        ({"updatedAt": "2020-11-08T00:00:00+00:60"}, "updatedAt"),
        ({"updatedAt": "2020-11-08T00:00:00+24:00"}, "updatedAt"),
        ({"updatedAt": "2020-11-08 00:00:00Z"}, "updatedAt"),
        ({"updatedAt": "２０２０-11-08T00:00:00Z"}, "updatedAt"),
        ({"type": {"group": "org.ga4gh", "artifact": "x"}}, "type"),
        ({"name": 1}, "name"),
    ],
)
def test_check_service_info_invalid(unit_app, fields, path):
    """Invalid fields are named."""
    _configure(unit_app, **fields)
    with (
        unit_app.app_context(),
        pytest.raises(
            InvalidServiceInfo, match=rf"`custom\.endpoints\.service_info\.{path}`"
        ),
    ):
        check_service_info()


@pytest.mark.parametrize(
    "fields",
    [
        {"contactUrl": "mailto:support@example.com"},
        {"createdAt": "2020-11-08T10:20:30.123+01:00"},
        {"updatedAt": "2020-11-08t10:20:30z"},
        {"updatedAt": "2020-02-29T10:20:30.1234567-05:30"},
        {"updatedAt": "2016-12-31T23:59:60Z"},
        {"x-custom": {"any": "value"}},
    ],
)
def test_check_service_info_valid(unit_app, fields):
    """URIs with any scheme, RFC 3339 date-times and extra fields are
    valid.
    """
    _configure(unit_app, **deepcopy(fields))
    with unit_app.app_context():
        check_service_info()


@pytest.mark.parametrize("check", [module._is_uri, module.is_date_time])
def test_format_checks_non_strings(check):
    """Format checks leave type checks to the schema."""
    assert check(1)
