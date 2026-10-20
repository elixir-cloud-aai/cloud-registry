"""Service info, from the config."""

from collections.abc import Callable
from datetime import date
from functools import cache
from importlib.metadata import PackageNotFoundError, version
import logging
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft4Validator, FormatChecker
from jsonschema.exceptions import best_match
import yaml

from cloud_registry.utils import get_custom_config

logger = logging.getLogger(__name__)
PACKAGE = "cloud-registry"
# GA4GH service-info specification with the `Service` schema
SPEC = Path(__file__).parents[2] / "api" / "20201108.11d2c12.service-info.yaml"
SCHEMA = "Service"
CONFIG_PATH = "custom.endpoints.service_info"
# Formats used by the `Service` schema; `jsonschema` checks them only if
# optional packages are installed (`rfc3987`, `rfc3339-validator`), so they
# are checked here, without more dependencies
FORMAT_CHECKER = FormatChecker(formats=())
# RFC 3986: a scheme, followed by anything without whitespace
URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S*")
# RFC 3339, section 5.6 (ASCII digits only; the RFC allows a space instead of
# `T` in a note only)
DATE_TIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[Tt]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d+)?"
    r"([Zz]|[+-](?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))",
    re.ASCII,
)


@FORMAT_CHECKER.checks("uri")
def _is_uri(value: object) -> bool:
    """Check whether a string is an absolute URI (non-strings pass)."""
    return not isinstance(value, str) or URI.fullmatch(value) is not None


@FORMAT_CHECKER.checks("date-time")
def _is_date_time(value: object) -> bool:
    """Check whether a string is an RFC 3339 date-time (non-strings pass)."""
    if not isinstance(value, str):
        return True
    match = DATE_TIME.fullmatch(value)
    if match is None:
        return False
    parts = {name: int(part or 0) for name, part in match.groupdict().items()}
    try:
        date(parts["year"], parts["month"], parts["day"])
    except ValueError:
        return False
    # Seconds may be 60 (leap seconds)
    return (
        parts["hour"] < 24
        and parts["minute"] < 60
        and parts["second"] <= 60
        and parts["offset_hour"] < 24
        and parts["offset_minute"] < 60
    )


class InvalidServiceInfo(ValueError):
    """The configured service info is invalid."""


def package_fields() -> dict[str, Any]:
    """Get service info fields from the package metadata.

    Returns:
        The package version as `version`, if the package is installed.
    """
    try:
        return {"version": version(PACKAGE)}
    except PackageNotFoundError:
        return {}


def get_service_info(
    runtime_fields: Callable[[], dict[str, Any]] = package_fields,
) -> dict[str, Any]:
    """Get the service info of the current app.

    Args:
        runtime_fields: Function returning fields set at runtime; the config
            takes precedence over them. Generic, so that this module can move
            to FOCA (elixir-cloud-aai/foca#314).

    Returns:
        Copy of the service info from the config (`custom.endpoints.service_info`), with
        the runtime fields it doesn't set, and without fields and list items
        set to `null` (config overlays can't remove fields otherwise).
    """
    return _without_nulls(
        {**runtime_fields(), **get_custom_config().endpoints.service_info}
    )


def _without_nulls(data: Any) -> Any:
    """Copy data without `None` values, recursively."""
    if isinstance(data, dict):
        return {
            key: _without_nulls(value)
            for key, value in data.items()
            if value is not None
        }
    if isinstance(data, list):
        return [_without_nulls(item) for item in data if item is not None]
    return data


def check_service_info(
    runtime_fields: Callable[[], dict[str, Any]] = package_fields,
) -> None:
    """Check the service info of the current app against the schema.

    The schema is the GA4GH `Service` schema of the bundled specification.

    Args:
        runtime_fields: Function returning fields set at runtime, as for
            `get_service_info()`.

    Raises:
        InvalidServiceInfo: The service info is invalid, naming the first
            invalid field.
    """
    info = get_service_info(runtime_fields=runtime_fields)
    error = best_match(_validator().iter_errors(info))
    if error is not None:
        field = ".".join(str(part) for part in (CONFIG_PATH, *error.absolute_path))
        raise InvalidServiceInfo(f"Invalid service info in `{field}`: {error.message}")
    logger.info("Serving service info '%s', version '%s'.", info["id"], info["version"])


@cache
def _validator() -> Draft4Validator:
    """Get a validator for the `Service` schema of the bundled specification.

    Returns:
        Validator resolving references within the specification.
    """
    with SPEC.open(encoding="utf-8") as spec_file:
        spec = yaml.safe_load(spec_file)
    schema = {
        "$ref": f"#/components/schemas/{SCHEMA}",
        "components": spec["components"],
    }
    return Draft4Validator(schema, format_checker=FORMAT_CHECKER)
