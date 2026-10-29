"""Unit tests for the service info fields of the registry."""

from flask import Flask
from foca import get_custom_config
from foca.config.access import get_app_config
from foca.models.config import ServiceInfoConfig
import pytest

from cloud_registry.ga4gh.registry.service_info import (
    RULES,
    RUNTIME_FIELDS,
    check_service_info_fields,
    registration_rules,
    runtime_fields,
)
from cloud_registry.service_models.custom_config import CustomConfig

# Service info as in `config.yaml`, with the fields required by the GA4GH
# `Service` schema
SERVICE: dict[str, object] = {
    "id": "org.example.cloud-registry",
    "name": "Cloud Registry",
    "type": {"group": "org.ga4gh", "artifact": "service-registry", "version": "1"},
    "organization": {"name": "Example Organization", "url": "https://example.org"},
    "version": "1.0.0",
}


def test_registration_rules_defaults(unit_app: Flask) -> None:
    """The rules are those of the default config."""
    with unit_app.app_context():
        assert registration_rules() == {
            "environments": ["prod", "test", "dev", "staging"],
            "url_schemes": ["https"],
            "service_types": "open",
            "max_unused_service_types": 10,
            "max_services_per_user": 50,
        }


def test_registration_rules_configured(unit_app: Flask) -> None:
    """The rules are those of the config in effect.

    No limits of unused types and services per user (`null`) leave out
    `max_unused_service_types` and `max_services_per_user`;
    an empty list of environments (any value) is served as it is.
    """
    with unit_app.app_context():
        custom = get_custom_config(CustomConfig)
        custom.endpoints.services.environments = []
        custom.endpoints.services.url_schemes = ["http", "https"]
        custom.access_control.service_types = "moderated"
        custom.access_control.max_unused_service_types = None
        custom.access_control.max_services_per_user = None
        assert registration_rules() == {
            "environments": [],
            "url_schemes": ["http", "https"],
            "service_types": "moderated",
        }
        custom.access_control.max_unused_service_types = 0
        assert registration_rules()["max_unused_service_types"] == 0
        custom.access_control.max_services_per_user = 0
        assert registration_rules()["max_services_per_user"] == 0


def test_registration_rules_copied(unit_app: Flask) -> None:
    """Changing the rules returned doesn't change the config."""
    with unit_app.app_context():
        rules = registration_rules()
        rules["environments"].append("x")
        rules["url_schemes"].append("http")
        assert registration_rules()["environments"] == [
            "prod",
            "test",
            "dev",
            "staging",
        ]
        assert registration_rules()["url_schemes"] == ["https"]


def test_runtime_fields(unit_app: Flask) -> None:
    """The runtime fields of the service info are the registration rules."""
    with unit_app.app_context():
        assert runtime_fields() == {RULES: registration_rules()}


def _set_service_info(
    app: Flask, service: dict[str, object], runtime: str | None
) -> None:
    """Set the service info config of an app.

    Args:
        app: Flask app.
        service: Service info (`service_info.service`).
        runtime: Path to the function returning fields at runtime
            (`service_info.runtime_fields`).
    """
    get_app_config(app).service_info = ServiceInfoConfig(
        service=service, runtime_fields=runtime
    )


def test_check_service_info_fields(unit_app: Flask) -> None:
    """The service info passes if the app adds the rules."""
    _set_service_info(unit_app, SERVICE, RUNTIME_FIELDS)
    with unit_app.app_context():
        check_service_info_fields()


@pytest.mark.parametrize(
    "service, runtime",
    [
        # the config sets the rules, which would take precedence
        ({**SERVICE, RULES: {"environments": []}}, RUNTIME_FIELDS),
        # also `null`, which would remove them
        ({**SERVICE, RULES: None}, RUNTIME_FIELDS),
        # the rules aren't added
        (SERVICE, None),
        (SERVICE, "foca.utils.service_info.without_nulls"),
    ],
)
def test_check_service_info_fields_invalid(
    unit_app: Flask, service: dict[str, object], runtime: str | None
) -> None:
    """The service info fails unless the app adds the rules, and only it."""
    _set_service_info(unit_app, service, runtime)
    with (
        unit_app.app_context(),
        pytest.raises(RuntimeError, match="registration_rules"),
    ):
        check_service_info_fields()


def test_check_service_info_fields_none(unit_app: Flask) -> None:
    """The service info fails if none is configured."""
    with (
        unit_app.app_context(),
        pytest.raises(RuntimeError, match="registration_rules"),
    ):
        check_service_info_fields()
