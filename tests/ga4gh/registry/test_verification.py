"""Unit tests for the controllers of verifications."""

from datetime import datetime, timezone

import pytest

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.service import (
    KEY,
    PROJECTION,
    VERIFIED,
    service_key,
)
from cloud_registry.ga4gh.registry.verification import (
    _format_time,
    _now,
    deleteServiceVerification,
    getServiceVerification,
    getVerifications,
    putServiceVerification,
)
from tests.mock_data import MOCK_ID, MOCK_SERVICE, MOCK_TYPE

# Time as read from the database (naive, UTC, in milliseconds)
TIME = datetime(2026, 10, 20, 12, 34, 56, 789000)
FORMATTED = "2026-10-20T12:34:56.789Z"
# URL and API type of the mock service, as checked by an admin
CHECKED = {"url": MOCK_SERVICE["url"], "type": MOCK_TYPE}


def _expected(verified_at=FORMATTED):
    """Expected verification of the mock service."""
    return {**CHECKED, "verified_at": verified_at}


def _insert(services, id=MOCK_ID, verified_at=None):
    """Insert the mock service; with its key for identifier `MOCK_ID` (keys
    are unique).
    """
    doc = {**MOCK_SERVICE, "id": id}
    if id == MOCK_ID:
        doc[KEY] = service_key(MOCK_SERVICE)
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
        assert getServiceVerification(MOCK_ID) == _expected()


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
    assert res == _expected(_format_time(stored))
    assert stored == datetime.fromisoformat(res["verified_at"][:-1])


@pytest.mark.parametrize(
    "checked",
    [
        CHECKED,
        # the version is not compared, the API type is case-insensitive
        {**CHECKED, "type": {**MOCK_TYPE, "version": "2.0.0"}},
        {**CHECKED, "type": {**MOCK_TYPE, "group": " ORG.GA4GH"}},
    ],
)
def test_putServiceVerification_checked(unit_app, services, checked):
    """Services with the URL and API type checked are verified."""
    _insert(services)
    with unit_app.test_request_context():
        res = putServiceVerification(MOCK_ID, verification=checked)
    stored = services.find_one({"id": MOCK_ID})[VERIFIED]
    assert res == _expected(_format_time(stored))


@pytest.mark.parametrize(
    "checked",
    [
        {**CHECKED, "url": "https://other.example.org/api"},
        # same key, but not the URL as registered
        {**CHECKED, "url": "https://beacon.example.org/api/"},
        {**CHECKED, "url": "https://beacon.example.org/api?a=b"},
        {**CHECKED, "type": {**MOCK_TYPE, "artifact": "tes"}},
    ],
)
def test_putServiceVerification_conflict(unit_app, services, checked):
    """Services with another URL or API type than the one checked are not
    verified, and their verification is unchanged.
    """
    _insert(services, verified_at=TIME)
    with unit_app.test_request_context():
        res = putServiceVerification(MOCK_ID, verification=checked)
    assert res.status_code == 409
    assert res.mimetype == "application/problem+json"
    assert "another URL or API type" in res.json["detail"]
    assert services.find_one({"id": MOCK_ID})[VERIFIED] == TIME


def test_putServiceVerification_invalid_url(unit_app, services):
    """Invalid URLs of the service checked are rejected, saying why."""
    _insert(services)
    checked = {**CHECKED, "url": "beacon.example.org/api"}
    with unit_app.test_request_context():
        res = putServiceVerification(MOCK_ID, verification=checked)
    assert res.status_code == 400
    assert "need a scheme and a host" in res.json["detail"]
    assert VERIFIED not in services.find_one({"id": MOCK_ID})


@pytest.mark.parametrize(
    "data, content_type",
    [
        ("text", "text/plain"),
        ("null", "text/plain"),
        ("{", "application/json"),
    ],
)
def test_putServiceVerification_unparsed_body(unit_app, services, data, content_type):
    """Requests with bodies that weren't parsed are rejected."""
    _insert(services)
    with unit_app.test_request_context(
        method="PUT", data=data, content_type=content_type
    ):
        res = putServiceVerification(MOCK_ID)
    assert res.status_code == 400
    assert VERIFIED not in services.find_one({"id": MOCK_ID})


@pytest.mark.parametrize("checked", [None, CHECKED])
def test_putServiceVerification_NotFound(unit_app, services, checked):
    with unit_app.test_request_context(), pytest.raises(NotFound):
        putServiceVerification(MOCK_ID, verification=checked)
    assert services.count_documents({}) == 0


# DELETE /services/{serviceId}/verification
def test_deleteServiceVerification(unit_app, services):
    _insert(services, verified_at=TIME)
    with unit_app.test_request_context():
        assert deleteServiceVerification(MOCK_ID) == MOCK_ID
    stored = services.find_one({"id": MOCK_ID}, {"_id": False})
    assert stored == {**MOCK_SERVICE, "id": MOCK_ID, KEY: service_key(MOCK_SERVICE)}


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
            {"service_id": "a", **_expected("2026-01-01T00:00:00.000Z")},
            {"service_id": "b", **_expected()},
        ]


def test_getVerifications_empty(unit_app, services):
    _insert(services)
    with unit_app.app_context():
        assert getVerifications() == []
