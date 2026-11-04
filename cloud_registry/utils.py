"""Typed access to the configuration and collections of the app."""

from datetime import UTC, date, datetime
import logging
import re
import time
from typing import Any

from foca import get_collection as get_foca_collection
from foca.database.register_mongodb import get_mongo_client, get_mongo_db_name
from foca.models.config import MongoConfig
from pymongo.collection import Collection
from pymongo.errors import OperationFailure

logger = logging.getLogger(__name__)
DB = "serviceStore"
# Codes of MongoDB's errors when others change a TTL index at the same time
# (cf. `set_ttl_index()`): namespace or index not found, index options or key
# specs conflict, and index build aborted
TTL_INDEX_RACES = frozenset({26, 27, 85, 86, 276})
TTL_INDEX_ATTEMPTS = 5
# Time to wait after the first attempt, in seconds; then longer each time
TTL_INDEX_WAIT = 0.2
# A field of each collection that versions before 1.0.0rc2 stored under another
# name, in documents they always stored it in (cf. `refuse_old_format()`)
OLD_FORMAT_FIELDS = {
    "invitations": "service_id",
    "user_names": "service_id",
    "service_types": "created_by",
}
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


def refuse_old_format(conf: MongoConfig) -> None:
    """Refuse to start on data stored by versions before 1.0.0rc2.

    These stored fields in `snake_case`, and there is no migration, so the app
    needs a fresh database (cf. "From 1.0.0rc1" in `docs/upgrading.md`). Only
    checks whether a document with an old field exists in the collections that
    have such fields, so call this on startup, before FOCA creates its indexes.

    Args:
        conf: Configuration of the app's MongoDB.

    Raises:
        RuntimeError: The database has data in the old format; the message
            names the collections.
    """
    database = get_mongo_client(conf)[get_mongo_db_name(DB)]
    found = [
        f"`{name}`"
        for name, field in OLD_FORMAT_FIELDS.items()
        if database[name].find_one({field: {"$exists": True}}, {"_id": True})
    ]
    if found:
        raise RuntimeError(
            f"The database '{DB}' has data stored by a version before 1.0.0rc2 "
            f"in {', '.join(found)}, which this version can't read. There is no "
            "migration: start with a fresh database (see "
            "https://cloud-registry.readthedocs.io/en/latest/upgrading/"
            "#from-100rc1)"
        )


def set_ttl_index(
    collection: Collection[dict[str, Any]], field: str, seconds: int
) -> None:
    """Make sure that a TTL index on a date field has the given seconds.

    For TTL indexes whose seconds come from a setting. MongoDB refuses to create
    an index with the same key and other options, so such an index can't be
    listed in FOCA's config, which only creates indexes; call this on startup,
    after FOCA has registered the database, so that changing the setting takes
    effect without manual steps. An existing index with other seconds is dropped
    and created again, as the role `readWrite` allows that, but not changing it
    in place (`collMod`); documents are only kept for longer meanwhile.

    Only handles the ascending index on the field with MongoDB's default name
    (`<field>_1`), which it owns: other indexes on the field need names of their
    own. Concurrent runs are safe (e.g., in each worker of the app on startup):
    runs interfering with each other (e.g., one dropping the index that another
    is building) try again, up to `TTL_INDEX_ATTEMPTS` times, and stop as soon as
    the index has the seconds. Of runs with different seconds, the last one wins.

    Args:
        collection: Collection.
        field: Name of the date field.
        seconds: Seconds after the date in the field at which MongoDB removes
            a document.

    Raises:
        pymongo.errors.OperationFailure: The index could not be dropped or
            created, e.g., for lack of permissions, or concurrent runs kept
            interfering.
    """
    name = f"{field}_1"
    for attempt in range(1, TTL_INDEX_ATTEMPTS + 1):
        index = collection.index_information().get(name)
        previous = None if index is None else index.get("expireAfterSeconds")
        if previous == seconds:
            return
        try:
            if index is not None:
                collection.drop_index(name)
            collection.create_index([(field, 1)], expireAfterSeconds=seconds)
        except OperationFailure as exc:
            if exc.code not in TTL_INDEX_RACES or attempt == TTL_INDEX_ATTEMPTS:
                raise
            time.sleep(TTL_INDEX_WAIT * attempt)
            continue
        if index is not None:
            logger.warning(
                "TTL index '%s' of collection '%s' changed from %s to %d seconds.",
                name,
                collection.name,
                previous,
                seconds,
            )
        return


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
