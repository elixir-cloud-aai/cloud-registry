"""Tests for custom config models."""

from pydantic import ValidationError
import pytest

from cloud_registry.service_models.custom_config import (
    AccessControlConfig,
    IdConfig,
    ServicesConfig,
)


@pytest.mark.parametrize("mode", ["open", "controlled"])
def test_access_control_service_types(mode: str) -> None:
    """Service types are open by default, or controlled."""
    assert AccessControlConfig().service_types == "open"
    config = AccessControlConfig.model_validate({"service_types": mode})
    assert config.service_types == mode


@pytest.mark.parametrize("mode", ["closed", "Open", "", None])
def test_access_control_invalid_service_types(mode: object) -> None:
    """Other modes of service types are rejected."""
    with pytest.raises(ValidationError):
        AccessControlConfig.model_validate({"service_types": mode})


def test_id_config() -> None:
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
def test_id_config_invalid_charset(charset: str) -> None:
    """Character sets unfit for identifiers are rejected.

    Rejected when the config is loaded: empty character sets, expressions
    (no longer evaluated since FOCA 0.14), and character sets giving
    identifiers that aren't URL path segments.
    """
    with pytest.raises(ValidationError):
        IdConfig(charset=charset, length=6)


@pytest.mark.parametrize("length", [0, -3])
def test_id_config_invalid_length(length: int) -> None:
    """Lengths that identifiers cannot be generated with are rejected."""
    with pytest.raises(ValidationError):
        IdConfig(charset="ABC123", length=length)


def test_services_config_url_schemes() -> None:
    """Service URLs must be `https` URLs by default; `http` can be allowed."""
    id_config = IdConfig(charset="ABC123", length=6)
    assert ServicesConfig(id=id_config).url_schemes == ["https"]
    schemes = ServicesConfig(id=id_config, url_schemes=["http", "https"]).url_schemes
    assert schemes == ["http", "https"]


def test_services_config_environments() -> None:
    """By default, the environments that the GA4GH specification suggests."""
    id_config = IdConfig(charset="ABC123", length=6)
    environments = ServicesConfig(id=id_config).environments
    assert environments == ["prod", "test", "dev", "staging"]
    assert ServicesConfig(id=id_config, environments=[]).environments == []


def test_services_config_environment_too_long() -> None:
    """Environments are at most as long as the API allows."""
    id_config = {"charset": "ABC123", "length": 6}
    assert ServicesConfig.model_validate(
        {"id": id_config, "environments": ["x" * 256]}
    ).environments == ["x" * 256]
    with pytest.raises(ValidationError):
        ServicesConfig.model_validate({"id": id_config, "environments": ["x" * 257]})


@pytest.mark.parametrize("schemes", [[], ["ftp"], ["HTTPS"], "https"])
def test_services_config_invalid_url_schemes(schemes: object) -> None:
    """Empty lists and schemes other than `http` and `https` are rejected."""
    with pytest.raises(ValidationError):
        ServicesConfig.model_validate(
            {"id": {"charset": "ABC123", "length": 6}, "url_schemes": schemes}
        )
