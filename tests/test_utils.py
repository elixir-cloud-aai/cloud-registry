"""Unit tests for `cloud_registry.utils`."""

from datetime import UTC, datetime
from typing import Any

from flask import Flask
from pymongo.collection import Collection
import pytest

from cloud_registry.utils import (
    format_time,
    get_collection,
    is_date_time,
    now,
)


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
