"""Typed access to the configuration and collections of the app."""

from datetime import UTC, date, datetime
import logging
import re
from typing import Any

from foca import get_collection as get_foca_collection
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)
DB = "serviceStore"
# Codes of MongoDB's errors when dropping an index that doesn't exist
INDEX_NOT_FOUND = 27
NAMESPACE_NOT_FOUND = 26
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


def set_ttl_index(
    collection: Collection[dict[str, Any]], field: str, seconds: int
) -> None:
    """Make sure that a TTL index on a date field has the given seconds.

    MongoDB refuses to create an index with the same key and other options, and
    so does FOCA, which only creates the indexes of its config: a TTL index
    whose seconds come from a setting can't be listed there. Instead, call this
    after FOCA has registered the database (e.g., on startup), so that changing
    the setting takes effect without manual steps. An index with other seconds
    is replaced by dropping and creating it, which also works for indexes
    created without seconds: documents are only kept for longer meanwhile.

    Concurrent runs with the same seconds are safe (e.g., in each worker of the
    app on startup). Only the index with the default name of the field is
    touched, so partial indexes on it can have names of their own.

    Args:
        collection: Collection.
        field: Name of the date field.
        seconds: Seconds after the date in the field at which MongoDB removes
            a document.

    Raises:
        pymongo.errors.OperationFailure: The index could not be dropped or
            created, e.g., for lack of permissions.
    """
    name = f"{field}_1"
    index = collection.index_information().get(name)
    if index is not None:
        if index.get("expireAfterSeconds") == seconds:
            return
        try:
            collection.drop_index(name)
        except OperationFailure as exc:
            if exc.code not in (INDEX_NOT_FOUND, NAMESPACE_NOT_FOUND):
                raise
    collection.create_index([(field, 1)], expireAfterSeconds=seconds)
    logger.info(
        "TTL index '%s' of collection '%s' set to %d seconds.",
        name,
        collection.name,
        seconds,
    )


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
