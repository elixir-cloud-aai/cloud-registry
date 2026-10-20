"""Service info, from the config."""

from collections.abc import Callable
from copy import deepcopy
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
# optional packages are installed
FORMAT_CHECKER = FormatChecker(formats=())
# RFC 3986: a scheme, followed by anything without whitespace
URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S*")
# RFC 3339, section 5.6
DATE_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})"
)


@FORMAT_CHECKER.checks("uri")
def _is_uri(value: object) -> bool:
    """Check whether a string is an absolute URI (non-strings pass)."""
    return not isinstance(value, str) or URI.fullmatch(value) is not None


@FORMAT_CHECKER.checks("date-time")
def _is_date_time(value: object) -> bool:
    """Check whether a string is an RFC 3339 date-time (non-strings pass)."""
    return not isinstance(value, str) or DATE_TIME.fullmatch(value) is not None


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

    Arguments:
        runtime_fields: Function returning fields set at runtime; the config
            takes precedence over them.

    Returns:
        Service info from the config (`custom.endpoints.service_info`), with
        the runtime fields it doesn't set.
    """
    return {**runtime_fields(), **deepcopy(get_custom_config().endpoints.service_info)}


def check_service_info(
    runtime_fields: Callable[[], dict[str, Any]] = package_fields,
) -> None:
    """Check the service info of the current app against the GA4GH `Service`
    schema.

    Arguments:
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
