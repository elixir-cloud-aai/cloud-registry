"""Unit tests for the controllers of verifications."""

from datetime import datetime, timezone

import pytest

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.service import PROJECTION, VERIFIED
from cloud_registry.ga4gh.registry.verification import (
    _format_time,
    _now,
    deleteServiceVerification,
    getServiceVerification,
    getVerifications,
    putServiceVerification,
)
from tests.mock_data import MOCK_ID, MOCK_SERVICE

# Time as read from the database (naive, UTC, in milliseconds)
TIME = datetime(2026, 10, 20, 12, 34, 56, 789000)
FORMATTED = "2026-10-20T12:34:56.789Z"


def _insert(services, id=MOCK_ID, verified_at=None):
    doc = {**MOCK_SERVICE, "id": id}
    if verified_at is not None:
        doc[VERIFIED] = verified_at
    services.insert_one(doc)


def test_now():
    """The current time is in UTC, in milliseconds."""
    now = _now()
    assert now.tzinfo == timezone.utc
    assert now.microsecond % 1000 == 0
    assert abs(datetime.now(timezone.utc) - now).total_seconds() < 5


@pytest.mark.parametrize(
    "time, formatted",
    [
        (TIME, FORMATTED),
        (TIME.replace(tzinfo=timezone.utc), FORMATTED),
        (datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05.000Z"),
    ],
)
def test_format_time(time, formatted):
    """Times are formatted as RFC 3339 date-times in UTC."""
    assert _format_time(time) == formatted


def test_verification_not_projected():
    """The verification is not served with services."""
    assert PROJECTION[VERIFIED] is False


# GET /services/{serviceId}/verification
def test_getServiceVerification(unit_app, services):
    _insert(services, verified_at=TIME)
    with unit_app.app_context():
        assert getServiceVerification(MOCK_ID) == {"verified_at": FORMATTED}


@pytest.mark.parametrize("insert", [False, True])
def test_getServiceVerification_NotFound(unit_app, services, insert):
    """Unknown and unverified services have no verification."""
    if insert:
        _insert(services)
    with unit_app.app_context(), pytest.raises(NotFound):
        getServiceVerification(MOCK_ID)


# PUT /services/{serviceId}/verification
def test_putServiceVerification(unit_app, services):
    _insert(services, verified_at=TIME)
    with unit_app.test_request_context():
        res = putServiceVerification(MOCK_ID)
    stored = services.find_one({"id": MOCK_ID})[VERIFIED]
    assert stored > TIME
    assert res == {"verified_at": _format_time(stored)}
    assert stored == datetime.fromisoformat(res["verified_at"][:-1])


def test_putServiceVerification_NotFound(unit_app, services):
    with unit_app.test_request_context(), pytest.raises(NotFound):
        putServiceVerification(MOCK_ID)
    assert services.count_documents({}) == 0


# DELETE /services/{serviceId}/verification
def test_deleteServiceVerification(unit_app, services):
    _insert(services, verified_at=TIME)
    with unit_app.test_request_context():
        assert deleteServiceVerification(MOCK_ID) == MOCK_ID
    stored = services.find_one({"id": MOCK_ID}, {"_id": False})
    assert stored == {**MOCK_SERVICE, "id": MOCK_ID}


@pytest.mark.parametrize("insert", [False, True])
def test_deleteServiceVerification_NotFound(unit_app, services, insert):
    if insert:
        _insert(services)
    with unit_app.test_request_context(), pytest.raises(NotFound):
        deleteServiceVerification(MOCK_ID)


# GET /verifications
def test_getVerifications(unit_app, services):
    """Verified services are listed, sorted by identifier."""
    _insert(services, id="b", verified_at=TIME)
    _insert(services, id="c")
    _insert(services, id="a", verified_at=datetime(2026, 1, 1))
    with unit_app.app_context():
        assert getVerifications() == [
            {"service_id": "a", "verified_at": "2026-01-01T00:00:00.000Z"},
            {"service_id": "b", "verified_at": FORMATTED},
        ]


def test_getVerifications_empty(unit_app, services):
    _insert(services)
    with unit_app.app_context():
        assert getVerifications() == []
