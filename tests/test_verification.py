"""Tests for verifications of services, with the app created from its config
(cf. `tests/conftest.py`).
"""

from copy import deepcopy
from datetime import datetime, timezone
from json import dumps

import pytest

from cloud_registry.ga4gh.registry import verification as verification_module
from tests.conftest import ADMIN, USER, make_headers

BASE = "/ga4gh/registry/v1"
SERVICE: dict = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
SERVICE_ID = "MYSERV"
OTHER = "bob@example.org"


def headers(user=None):
    """Request headers with an access token for a user; none if `None`."""
    return {} if user is None else make_headers(user)


def _service(**kwargs):
    service = deepcopy(SERVICE)
    service.update(kwargs)
    return service


def put_service(client, service=None, user=ADMIN, service_id=SERVICE_ID):
    """Register or replace a service; return the status code."""
    return client.put(
        f"{BASE}/services/{service_id}",
        json=_service() if service is None else service,
        headers=headers(user),
    ).status_code


def verification(client, service_id=SERVICE_ID, method="get", user=None, **kwargs):
    """Send a request to the verification of a service."""
    return client.open(
        f"{BASE}/services/{service_id}/verification",
        method=method,
        headers=headers(user),
        **kwargs,
    )


def verify(client, service_id=SERVICE_ID, user=ADMIN, checked=None):
    """Verify a service, with the URL and API type checked, if given."""
    kwargs = {} if checked is None else {"json": checked}
    return verification(client, service_id, method="put", user=user, **kwargs)


def checked(service=SERVICE):
    """URL and API type of a service, as checked by an admin."""
    return {"url": service["url"], "type": deepcopy(service["type"])}


def expected(verified_at, service=SERVICE):
    """Expected verification of a service."""
    return {**checked(service), "verified_at": verified_at}


def unverify(client, service_id=SERVICE_ID, user=ADMIN):
    return verification(client, service_id, method="delete", user=user)


def verified(client):
    """Identifiers of the verified services."""
    response = client.get(f"{BASE}/verifications")
    assert response.status_code == 200
    return [item["service_id"] for item in response.json]


@pytest.fixture
def service(client):
    """Service registered by `USER` (its owner) with identifier
    `SERVICE_ID`.
    """
    assert put_service(client) == 201
    # make the user owner, as if they had registered it
    response = client.put(
        f"{BASE}/services/{SERVICE_ID}/permissions/{USER}",
        json={"role": "owner"},
        headers=headers(ADMIN),
    )
    assert response.status_code == 200
    return SERVICE_ID


def test_verify(client, service):
    """Admins verify services; anyone can read the verification."""
    before = datetime.now(timezone.utc).replace(microsecond=0)
    response = verify(client)
    assert response.status_code == 200
    verified_at = response.json["verified_at"]
    assert datetime.fromisoformat(verified_at.replace("Z", "+00:00")) >= before

    assert response.json == expected(verified_at)

    response = verification(client)
    assert response.status_code == 200
    assert response.json == expected(verified_at)
    response = client.get(f"{BASE}/verifications")
    assert response.json == [{"service_id": service, **expected(verified_at)}]


@pytest.mark.parametrize(
    "body",
    [
        checked(),
        # the version is not compared, the API type is case-insensitive
        {**checked(), "type": {**SERVICE["type"], "version": "9.9.9"}},
        {**checked(), "type": {**SERVICE["type"], "artifact": "TES"}},
    ],
)
def test_verify_checked(client, service, body):
    """Services are verified if they have the URL and API type checked."""
    response = verify(client, checked=body)
    assert response.status_code == 200
    assert response.json == expected(response.json["verified_at"])
    assert verified(client) == [service]


@pytest.mark.parametrize(
    "body",
    [
        {**checked(), "url": "https://other.example.org/ga4gh/tes/v1"},
        # same normalized URL, but not as registered
        {**checked(), "url": "https://tes.example.org/ga4gh/tes/v1/"},
        {**checked(), "url": "https://tes.example.org/ga4gh/tes/v1?a=b"},
        {**checked(), "type": {**SERVICE["type"], "artifact": "wes"}},
        {**checked(), "type": {**SERVICE["type"], "group": "org.example"}},
    ],
)
def test_verify_checked_conflict(client, service, body):
    """Services with another URL or API type than the one checked are not
    verified, saying why; verifications are unchanged.
    """
    response = verify(client, checked=body)
    assert response.status_code == 409
    assert response.mimetype == "application/problem+json"
    assert "another URL or API type" in response.json["detail"]
    assert verified(client) == []
    verified_at = verify(client).json["verified_at"]
    assert verify(client, checked=body).status_code == 409
    assert verification(client).json == expected(verified_at)


def test_verify_checked_not_found(client):
    """Unknown services can't be verified, also with a body."""
    assert verify(client, "UNKNWN", checked=checked()).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"url": SERVICE["url"]},
        {"type": SERVICE["type"]},
        {**checked(), "name": "My TES"},
        {**checked(), "type": {"group": "org.ga4gh", "artifact": "tes"}},
        # longer than the length limits
        {**checked(), "url": "https://tes.example.org/" + "a" * 2025},
        {**checked(), "type": {**SERVICE["type"], "artifact": "a" * 257}},
    ],
)
def test_verify_checked_invalid(client, service, body):
    """Bodies need exactly a URL and an API type."""
    assert verify(client, checked=body).status_code == 400
    assert verified(client) == []


@pytest.mark.parametrize(
    "url, detail",
    [
        ("https://user@tes.example.org/ga4gh/tes/v1", "must not contain user info"),
        ("tes.example.org/ga4gh/tes/v1", "need a scheme and a host"),
    ],
)
def test_verify_checked_invalid_url(client, service, url, detail):
    """Invalid URLs in the body are rejected, saying why."""
    response = verify(client, checked={**checked(), "url": url})
    assert response.status_code == 400
    assert response.mimetype == "application/problem+json"
    assert detail in response.json["detail"]
    assert verified(client) == []


@pytest.mark.parametrize(
    "data, content_type",
    [
        ("url=https://tes.example.org", "application/x-www-form-urlencoded"),
        (dumps(checked()), "text/plain"),
        (dumps(checked()), None),
        ('{"url": ', "application/json"),
        ("null", "text/plain"),
        # form data without fields
        ("&", "application/x-www-form-urlencoded"),
        ("a=", "application/x-www-form-urlencoded"),
        ("&", None),
    ],
)
def test_verify_unparsed_body(client, service, data, content_type):
    """Bodies that aren't JSON are rejected, rather than verifying the
    service unconditionally.
    """
    headers = make_headers(ADMIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.put(
        f"{BASE}/services/{service}/verification", data=data, headers=headers
    )
    assert response.status_code == 400
    assert response.mimetype == "application/problem+json"
    assert verified(client) == []


@pytest.mark.parametrize(
    "data, content_type",
    [
        ("", None),
        ("", "application/json"),
        (" ", "application/json"),
        ("null", "application/json"),
    ],
)
def test_verify_empty_body(client, service, data, content_type):
    """Empty bodies and `null` verify the service as registered."""
    headers = make_headers(ADMIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.put(
        f"{BASE}/services/{service}/verification", data=data, headers=headers
    )
    assert response.status_code == 200
    assert verified(client) == [service]


def _set_clock(monkeypatch, *times):
    """Let verifications happen at the given times, in this order."""
    clock = iter(times)
    monkeypatch.setattr(verification_module, "_now", lambda: next(clock))


def test_verify_again(client, service, monkeypatch):
    """Verifying a verified service renews its verification."""
    _set_clock(
        monkeypatch,
        datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
        datetime(2026, 10, 20, 13, tzinfo=timezone.utc),
    )
    assert verify(client).json["verified_at"] == "2026-10-20T12:00:00.000Z"
    second = verify(client)
    assert second.status_code == 200
    assert second.json["verified_at"] == "2026-10-20T13:00:00.000Z"
    assert verification(client).json == second.json
    assert verified(client) == [service]


def test_verify_again_earlier(client, service, monkeypatch):
    """Renewals at an earlier time (e.g., of an instance with a clock behind)
    keep the later time, which is returned.
    """
    _set_clock(
        monkeypatch,
        datetime(2026, 10, 20, 13, tzinfo=timezone.utc),
        datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
    )
    first = verify(client).json
    second = verify(client)
    assert second.status_code == 200
    assert second.json == first
    assert verification(client).json == first


def test_verification_not_served(client, service):
    """Services are served without verification, as by the GA4GH
    specification.
    """
    assert verify(client).status_code == 200
    assert client.get(f"{BASE}/services/{service}").json == _service(id=service)
    assert client.get(f"{BASE}/services").json == [_service(id=service)]


@pytest.mark.parametrize("user", [None, USER, OTHER])
def test_only_admins_verify(client, service, user):
    """Only admins can verify services and remove verifications, not their
    owners or other users.
    """
    status = 401 if user is None else 403
    assert verify(client, user=user).status_code == status
    assert verified(client) == []
    assert verify(client).status_code == 200
    assert unverify(client, user=user).status_code == status
    assert verified(client) == [service]


@pytest.mark.parametrize("user", [None, USER])
def test_public_read(client, service, user):
    """Anyone can read verifications, also without an access token."""
    assert verify(client).status_code == 200
    assert verification(client, user=user).status_code == 200
    response = client.get(f"{BASE}/verifications", headers=headers(user))
    assert response.status_code == 200


def test_unverify(client, service):
    """Admins remove verifications; removing them again returns 404."""
    assert verify(client).status_code == 200
    response = unverify(client)
    assert response.status_code == 200
    assert response.json == service
    assert verification(client).status_code == 404
    assert verified(client) == []
    assert unverify(client).status_code == 404


def test_not_found(client):
    """Unknown and unverified services have no verification."""
    for method in ["get", "put", "delete"]:
        response = verification(client, "UNKNWN", method=method, user=ADMIN)
        assert response.status_code == 404
        assert response.mimetype == "application/problem+json"
    assert put_service(client) == 201
    assert verification(client).status_code == 404
    assert unverify(client).status_code == 404


def test_list(client):
    """Only verified services are listed, sorted by identifier."""
    for i, service_id in enumerate(["B", "C", "A"]):
        service = _service(url=f"https://tes{i}.example.org")
        assert put_service(client, service, service_id=service_id) == 201
    assert verified(client) == []
    assert verify(client, "C").status_code == 200
    assert verify(client, "A").status_code == 200
    assert verified(client) == ["A", "C"]


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://other.example.org/ga4gh/tes/v1"},
        {"type": {**SERVICE["type"], "artifact": "wes"}},
        {"type": {**SERVICE["type"], "group": "org.example"}},
        # same normalized URL (and so the same service), but spelled
        # differently, e.g., with a query
        {"url": "HTTPS://TES.example.org:443/ga4gh/tes/v1/"},
        {"url": "https://tes.example.org/ga4gh/tes/v1?redirect=evil"},
        {"url": "https://tes.example.org/ga4gh/tes/v1#evil"},
    ],
)
def test_reset_on_change(client, service, change):
    """Changing the URL (as written) or API type of a service removes its
    verification.
    """
    assert verify(client).status_code == 200
    assert put_service(client, _service(**change), user=USER) == 200
    assert verification(client).status_code == 404
    assert verified(client) == []
    # changing it back doesn't restore it
    assert put_service(client, user=USER) == 200
    assert verification(client).status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"type": {**SERVICE["type"], "version": "2.0.0"}},
        {"version": "2.0.0", "name": "New name"},
        # same API type, in another spelling
        {"type": {**SERVICE["type"], "artifact": " TES "}},
    ],
)
def test_kept_on_update(client, service, change):
    """Updating a service with the same URL and API type keeps its
    verification, served with the URL and API type as updated.
    """
    verified_at = verify(client).json["verified_at"]
    assert put_service(client, _service(**change), user=USER) == 200
    assert verification(client).json == expected(verified_at, _service(**change))
    assert client.get(f"{BASE}/services/{service}").json == _service(
        id=service, **change
    )


def test_removed_with_service(client, service):
    """Deleting a service deletes its verification; a new service with the
    identifier is not verified.
    """
    assert verify(client).status_code == 200
    response = client.delete(f"{BASE}/services/{service}", headers=headers(USER))
    assert response.status_code == 200
    assert verified(client) == []
    assert put_service(client) == 201
    assert verification(client).status_code == 404


def test_rejected_update_keeps_verification(client, service):
    """Updates rejected as duplicates keep the verification."""
    assert verify(client).status_code == 200
    other = _service(url="https://other.example.org")
    assert put_service(client, other, service_id="OTHER") == 201
    assert put_service(client, other, user=USER) == 409
    assert verified(client) == [service]


def test_spec(client):
    """The GA4GH endpoints and schemas are unchanged: verifications are
    served by endpoints of their own.
    """
    spec = client.get(f"{BASE}/openapi.json").json
    ga4gh = [
        spec["paths"]["/services"]["get"],
        spec["paths"]["/services/{serviceId}"]["get"],
        spec["components"]["schemas"]["ExternalService"],
    ]
    assert "verif" not in dumps(ga4gh).lower()


def test_logged_values_escaped(client, caplog):
    """Values from request paths are logged with control characters escaped,
    so that they can't forge or hide log lines (line breaks are rejected in
    identifiers, as they can't be stored in policy rules).
    """
    caplog.set_level("INFO")
    service_id = "MY%1BSERV"  # with an escape character
    assert put_service(client, service_id=service_id) == 201
    assert verify(client, service_id=service_id).status_code == 200
    assert "Service 'MY\\x1bSERV' verified by" in caplog.text
    assert "MY\x1bSERV" not in caplog.text


def test_verification_changes_logged(client, caplog):
    """Verifications, and their removal by admins or updates, are logged with
    the user who changed them.
    """
    caplog.set_level("INFO")
    assert put_service(client) == 201
    assert verify(client).status_code == 200
    assert put_service(client, _service(url="https://other.example.org")) == 200
    assert verify(client).status_code == 200
    assert unverify(client).status_code == 200
    messages = [
        f"Service '{SERVICE_ID}' verified by '{ADMIN}'.",
        f"Verification of service '{SERVICE_ID}' removed by an update by "
        f"'{ADMIN}' (URL or API type changed).",
        f"Verification of service '{SERVICE_ID}' removed by '{ADMIN}'.",
    ]
    for message in messages:
        assert message in caplog.text
