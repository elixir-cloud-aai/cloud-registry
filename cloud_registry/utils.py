"""Typed access to the configuration and collections of the app."""

from datetime import UTC, date, datetime
import re
from typing import Any

from foca import get_collection as get_foca_collection
from pymongo.collection import Collection

DB = "serviceStore"
# RFC 3339, section 5.6 (ASCII digits only; the RFC allows a space instead of
# `T` in a note only)
DATE_TIME = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[Tt]"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d+)?"
    r"([Zz]|[+-](?P<offset_hour>\d{2}):(?P<offset_minute>\d{2}))",
    re.ASCII,
)


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
