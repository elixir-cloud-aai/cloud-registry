"""Typed access to the configuration and collections of the app."""

from datetime import UTC, date, datetime
import ipaddress
import re
from typing import Any
from urllib.parse import quote

from flask import request
from foca import get_collection as get_foca_collection
from foca import get_config
from pymongo.collection import Collection

from cloud_registry.service_models.custom_config import CustomConfig

DB = "serviceStore"
# RFC 3339, section 5.6 (ASCII digits only; the RFC allows a space instead of
# `T` in a note only)
DATE_TIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[Tt]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d+)?"
    r"([Zz]|[+-](?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))",
    re.ASCII,
)


def get_custom_config() -> CustomConfig:
    """Get the custom configuration of the current app.

    Returns:
        Custom configuration.

    Raises:
        RuntimeError: The app has no custom configuration.
    """
    # FOCA keeps the custom configuration as an untyped extra field; it is an
    # instance of `CustomConfig` only if the app loads the model by its full
    # path (`cloud_registry.service_models.custom_config.CustomConfig`)
    custom = (get_config().model_extra or {}).get("custom")
    if not isinstance(custom, CustomConfig):
        raise RuntimeError(
            f"No custom configuration of type {CustomConfig}; check that "
            "`custom_config_model` is its full module path"
        )
    return custom


def get_public_url(path: str) -> str:
    """Get the public URL of a path of the current app.

    Must be called in a request context. The URL is built from
    `custom.endpoints.service`, with the path prefix `api_path` (e.g., of a
    reverse proxy) in front of the root path of the app and the path.

    Args:
        path: Path below the root path of the app (e.g., the request path),
            not percent-encoded; empty for the root path.

    Returns:
        Public URL, with the root path of the app and the path percent-encoded,
        and without trailing slash, unless it is the URL of the host's root
        (e.g., for an empty path, `api_path` and root path of the app).
    """
    conf = get_custom_config().endpoints.service
    host = _format_host(conf.external_host)
    # `api_path` is configured as it appears in URLs, so it isn't encoded
    parts = (conf.api_path, quote(request.script_root + path))
    url_path = "/".join(filter(None, (part.strip("/") for part in parts)))
    return f"{conf.url_prefix}://{host}:{conf.external_port}/{url_path}"


def _format_host(host: str) -> str:
    """Format a host for URLs.

    Args:
        host: Host name or IP address; IPv6 addresses with or without
            brackets (with brackets, zone IDs percent-encoded, as in URLs).

    Returns:
        Host; IPv6 addresses in brackets, with zone IDs percent-encoded (RFC
        6874).
    """
    bracketed = host.startswith("[") and host.endswith("]")
    address, _, zone = host.strip("[]").partition("%")
    if bracketed:
        # as in URLs: zone IDs percent-encoded
        zone = zone.removeprefix("25")
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return host
    return f"[{address}%25{zone}]" if zone else f"[{address}]"


def get_collection(name: str) -> Collection[dict[str, Any]]:
    """Get a collection of the service store of the current app.

    Args:
        name: Name of the collection, as configured.

    Returns:
        MongoDB collection.
    """
    return get_foca_collection(db=DB, collection=name)


def is_date_time(value: object) -> bool:
    """Check whether a string is an RFC 3339 date-time (non-strings pass).

    Used for the timestamps of registered services (cf.
    `cloud_registry.ga4gh.registry.service.check_date_times()`).
    """
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


def now() -> datetime:
    """Get the current time, in the precision of the database.

    Returns:
        Current time (UTC), in milliseconds, like MongoDB dates.
    """
    time = datetime.now(UTC)
    return time.replace(microsecond=time.microsecond // 1000 * 1000)


def format_time(time: datetime) -> str:
    """Format a time as an RFC 3339 date-time in UTC.

    Used for the times of verifications and the timestamps of services.

    Args:
        time: Time in UTC; naive, as read from the database, or aware.

    Returns:
        RFC 3339 date-time in UTC, in milliseconds, e.g.,
        `2026-10-20T12:34:56.789Z`.
    """
    return f"{time:%Y-%m-%dT%H:%M:%S}.{time.microsecond // 1000:03d}Z"
