"""Unit tests for `cloud_registry.utils`."""

from datetime import UTC, datetime
from typing import Any

from flask import Flask
from pymongo.collection import Collection
from pymongo.errors import OperationFailure
import pytest

from cloud_registry.utils import (
    format_time,
    get_collection,
    is_date_time,
    now,
    set_ttl_index,
)
from tests.conftest import COLLECTION_CLASS


def _seconds(collection: Collection[dict[str, Any]]) -> int | None:
    """Get the seconds of the index on the field `expires`.

    Args:
        collection: Collection.

    Returns:
        Value of `expireAfterSeconds`, if any.
    """
    index: dict[str, Any] = collection.index_information()["expires_1"]
    return index.get("expireAfterSeconds")


def test_set_ttl_index(services: Collection[dict[str, Any]]) -> None:
    """TTL indexes are created, and changed if their seconds differ.

    Also indexes on the field without seconds. Documents are kept, as are
    other indexes on the field.
    """
    services.insert_one({"_id": "kept"})
    services.create_index(
        [("expires", 1)],
        name="partial",
        partialFilterExpression={"x": {"$exists": True}},
    )
    set_ttl_index(services, "expires", 60)
    assert _seconds(services) == 60
    set_ttl_index(services, "expires", 0)
    assert _seconds(services) == 0
    set_ttl_index(services, "expires", 120)
    assert _seconds(services) == 120
    assert services.count_documents({"_id": "kept"}) == 1
    assert "partial" in services.index_information()
    services.drop_index("expires_1")
    services.create_index([("expires", 1)])
    set_ttl_index(services, "expires", 60)
    assert _seconds(services) == 60


def test_set_ttl_index_unchanged(
    services: Collection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL indexes with the given seconds are left alone."""
    set_ttl_index(services, "expires", 60)

    def fail(*args: Any, **kwargs: Any) -> None:
        """Fail if an index is dropped.

        Args:
            args: Arguments; unused.
            kwargs: Keyword arguments; unused.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("dropped")

    monkeypatch.setattr(COLLECTION_CLASS, "drop_index", fail)
    set_ttl_index(services, "expires", 60)
    assert _seconds(services) == 60


@pytest.mark.parametrize("code", [26, 27])
def test_set_ttl_index_dropped_meanwhile(
    services: Collection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    """An index that another worker dropped meanwhile is created anyway."""
    set_ttl_index(services, "expires", 60)
    original = COLLECTION_CLASS.drop_index

    def drop_twice(self: Any, *args: Any, **kwargs: Any) -> None:
        """Drop the index, then fail as if another worker had done so.

        Args:
            self: Collection.
            args: Arguments of `drop_index()`.
            kwargs: Keyword arguments of `drop_index()`.

        Raises:
            OperationFailure: Always.
        """
        original(self, *args, **kwargs)
        raise OperationFailure("gone", code=code)

    monkeypatch.setattr(COLLECTION_CLASS, "drop_index", drop_twice)
    set_ttl_index(services, "expires", 120)
    assert _seconds(services) == 120


def test_set_ttl_index_drop_fails(
    services: Collection[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other failures of dropping the index are raised, e.g., permissions."""
    set_ttl_index(services, "expires", 60)

    def refuse(*args: Any, **kwargs: Any) -> None:
        """Refuse to drop the index.

        Args:
            args: Arguments; unused.
            kwargs: Keyword arguments; unused.

        Raises:
            OperationFailure: Always.
        """
        raise OperationFailure("unauthorized", code=13)

    monkeypatch.setattr(COLLECTION_CLASS, "drop_index", refuse)
    with pytest.raises(OperationFailure, match="unauthorized"):
        set_ttl_index(services, "expires", 120)


def test_get_collection(unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
    """Configured collections of the service store are returned."""
    with unit_app.app_context():
        assert get_collection("services") is services
        with pytest.raises(KeyError):
            get_collection("unknown")


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


# Time as read from the database (naive, UTC, in milliseconds)
DB_TIME = datetime(2026, 10, 20, 12, 34, 56, 789000)
FORMATTED_DB_TIME = "2026-10-20T12:34:56.789Z"


def test_now() -> None:
    """The current time is in UTC, in milliseconds."""
    time = now()
    assert time.tzinfo == UTC
    assert time.microsecond % 1000 == 0
    assert abs(datetime.now(UTC) - time).total_seconds() < 5


@pytest.mark.parametrize(
    "time, formatted",
    [
        (DB_TIME, FORMATTED_DB_TIME),
        (DB_TIME.replace(tzinfo=UTC), FORMATTED_DB_TIME),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05.000Z"),
    ],
)
def test_format_time(time: datetime, formatted: str) -> None:
    """Times are formatted as RFC 3339 date-times in UTC."""
    assert format_time(time) == formatted
