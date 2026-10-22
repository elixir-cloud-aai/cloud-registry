"""Tests for verifications of services, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`.
"""

from copy import deepcopy
from datetime import UTC, datetime
from json import dumps
from typing import Any

from flask.testing import FlaskClient
import pytest
from werkzeug.test import TestResponse

from cloud_registry.ga4gh.registry import verification as verification_module
from tests.conftest import ADMIN, USER, make_headers
from tests.mock_data import ERROR_MEDIA_TYPE

BASE = "/ga4gh/registry/v1"
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
SERVICE_ID = "MYSERV"
OTHER = "bob@example.org"


def headers(user: str | None = None) -> dict[str, str]:
    """Get request headers with an access token for a user.

    Args:
        user: User identifier; no token if `None`.

    Returns:
        Request headers.
    """
    return {} if user is None else make_headers(user)


def _service(**kwargs: Any) -> dict[str, Any]:
    """Get a copy of the test service, with fields changed.

    Args:
        kwargs: Fields to set.

    Returns:
        The test service, with the fields set.
    """
    service = deepcopy(SERVICE)
    service.update(kwargs)
    return service


def put_service(
    client: FlaskClient,
    service: dict[str, Any] | None = None,
    user: str = ADMIN,
    service_id: str = SERVICE_ID,
) -> int:
    """Register or replace a service.

    Args:
        client: Test client of the app.
        service: Service; the test service if `None`.
        user: User who sends the request.
        service_id: Service identifier.

    Returns:
        Status code of the response.
    """
    return client.put(
        f"{BASE}/services/{service_id}",
        json=_service() if service is None else service,
        headers=headers(user),
    ).status_code


def verification(
    client: FlaskClient,
    service_id: str = SERVICE_ID,
    method: str = "get",
    user: str | None = None,
    **kwargs: Any,
) -> TestResponse:
    """Send a request to the verification of a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        method: HTTP method.
        user: User who sends the request; no token if `None`.
        kwargs: Other arguments of the request (e.g., `json`).

    Returns:
        Response.
    """
    return client.open(
        f"{BASE}/services/{service_id}/verification",
        method=method,
        headers=headers(user),
        **kwargs,
    )


def verify(
    client: FlaskClient,
    service_id: str = SERVICE_ID,
    user: str | None = ADMIN,
    checked: dict[str, Any] | None = None,
) -> TestResponse:
    """Verify a service, with the URL and API type checked, if given.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who verifies the service; no token if `None`.
        checked: URL and API type checked, as request body; none if `None`.

    Returns:
        Response.
    """
    kwargs = {} if checked is None else {"json": checked}
    return verification(client, service_id, method="put", user=user, **kwargs)


def checked(service: dict[str, Any] = SERVICE) -> dict[str, Any]:
    """Get the URL and API type of a service, as checked by an admin.

    Args:
        service: Service.

    Returns:
        URL and API type of the service.
    """
    return {"url": service["url"], "type": deepcopy(service["type"])}


def expected(verified_at: str, service: dict[str, Any] = SERVICE) -> dict[str, Any]:
    """Get the expected verification of a service.

    Args:
        verified_at: Time of the verification, as served.
        service: Service.

    Returns:
        Verification of the service.
    """
    return {**checked(service), "verified_at": verified_at}


def unverify(
    client: FlaskClient, service_id: str = SERVICE_ID, user: str | None = ADMIN
) -> TestResponse:
    """Remove the verification of a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who removes the verification; no token if `None`.

    Returns:
        Response.
    """
    return verification(client, service_id, method="delete", user=user)


def verified(client: FlaskClient) -> list[str]:
    """Get the identifiers of the verified services.

    Args:
        client: Test client of the app.

    Returns:
        Identifiers of the verified services, as listed.
    """
    response = client.get(f"{BASE}/verifications")
    assert response.status_code == 200
    return [item["service_id"] for item in response.get_json()]


@pytest.fixture
def service(client: FlaskClient) -> str:
    """Service registered by `USER` (its owner) with identifier `SERVICE_ID`."""
    assert put_service(client) == 201
    # make the user owner, as if they had registered it
    response = client.put(
        f"{BASE}/services/{SERVICE_ID}/permissions/{USER}",
        json={"role": "owner"},
        headers=headers(ADMIN),
    )
    assert response.status_code == 200
    return SERVICE_ID


def test_verify(client: FlaskClient, service: str) -> None:
    """Admins verify services; anyone can read the verification."""
    before = datetime.now(UTC).replace(microsecond=0)
    response = verify(client)
    assert response.status_code == 200
    verified_at = response.get_json()["verified_at"]
    assert datetime.fromisoformat(verified_at.replace("Z", "+00:00")) >= before

    assert response.get_json() == expected(verified_at)

    response = verification(client)
    assert response.status_code == 200
    assert response.get_json() == expected(verified_at)
    response = client.get(f"{BASE}/verifications")
    assert response.get_json() == [{"service_id": service, **expected(verified_at)}]


@pytest.mark.parametrize(
    "body",
    [
        checked(),
        # the version is not compared, the API type is case-insensitive
        {**checked(), "type": {**SERVICE["type"], "version": "9.9.9"}},
        {**checked(), "type": {**SERVICE["type"], "artifact": "TES"}},
    ],
)
def test_verify_checked(
    client: FlaskClient, service: str, body: dict[str, Any]
) -> None:
    """Services are verified if they have the URL and API type checked."""
    response = verify(client, checked=body)
    assert response.status_code == 200
    assert response.get_json() == expected(response.get_json()["verified_at"])
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
def test_verify_checked_conflict(
    client: FlaskClient, service: str, body: dict[str, Any]
) -> None:
    """Services with another URL or API type than the one checked aren't verified.

    The response says why; verifications are unchanged.
    """
    response = verify(client, checked=body)
    assert response.status_code == 409
    assert response.content_type == ERROR_MEDIA_TYPE
    assert "another URL or API type" in response.get_json()["detail"]
    assert verified(client) == []
    verified_at = verify(client).get_json()["verified_at"]
    assert verify(client, checked=body).status_code == 409
    assert verification(client).get_json() == expected(verified_at)


def test_verify_checked_not_found(client: FlaskClient) -> None:
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
def test_verify_checked_invalid(
    client: FlaskClient, service: str, body: dict[str, Any]
) -> None:
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
def test_verify_checked_invalid_url(
    client: FlaskClient, service: str, url: str, detail: str
) -> None:
    """Invalid URLs in the body are rejected, saying why."""
    response = verify(client, checked={**checked(), "url": url})
    assert response.status_code == 400
    assert response.content_type == ERROR_MEDIA_TYPE
    assert detail in response.get_json()["detail"]
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
def test_verify_unparsed_body(
    client: FlaskClient, service: str, data: str, content_type: str | None
) -> None:
    """Bodies that aren't JSON are rejected.

    They don't verify the service unconditionally.
    """
    headers = make_headers(ADMIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.put(
        f"{BASE}/services/{service}/verification", data=data, headers=headers
    )
    assert response.status_code == 400
    assert response.content_type == ERROR_MEDIA_TYPE
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
def test_verify_empty_body(
    client: FlaskClient, service: str, data: str, content_type: str | None
) -> None:
    """Empty bodies and `null` verify the service as registered."""
    headers = make_headers(ADMIN)
    if content_type is not None:
        headers["Content-Type"] = content_type
    response = client.put(
        f"{BASE}/services/{service}/verification", data=data, headers=headers
    )
    assert response.status_code == 200
    assert verified(client) == [service]


def _set_clock(monkeypatch: pytest.MonkeyPatch, *times: datetime) -> None:
    """Let verifications happen at the given times, in this order.

    Args:
        monkeypatch: Monkeypatch to patch the clock with.
        times: Times of the verifications.
    """
    clock = iter(times)
    monkeypatch.setattr(verification_module, "_now", lambda: next(clock))


def test_verify_again(
    client: FlaskClient, service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifying a verified service renews its verification."""
    _set_clock(
        monkeypatch,
        datetime(2026, 10, 20, 12, tzinfo=UTC),
        datetime(2026, 10, 20, 13, tzinfo=UTC),
    )
    assert verify(client).get_json()["verified_at"] == "2026-10-20T12:00:00.000Z"
    second = verify(client)
    assert second.status_code == 200
    assert second.get_json()["verified_at"] == "2026-10-20T13:00:00.000Z"
    assert verification(client).get_json() == second.get_json()
    assert verified(client) == [service]


def test_verify_again_earlier(
    client: FlaskClient, service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewals at an earlier time keep the later time, which is returned.

    E.g., renewals by an instance with a clock behind.
    """
    _set_clock(
        monkeypatch,
        datetime(2026, 10, 20, 13, tzinfo=UTC),
        datetime(2026, 10, 20, 12, tzinfo=UTC),
    )
    first = verify(client).get_json()
    second = verify(client)
    assert second.status_code == 200
    assert second.get_json() == first
    assert verification(client).get_json() == first


def test_verification_not_served(client: FlaskClient, service: str) -> None:
    """Services are served without verification, as by the GA4GH specification."""
    assert verify(client).status_code == 200
    assert client.get(f"{BASE}/services/{service}").get_json() == _service(id=service)
    assert client.get(f"{BASE}/services").get_json() == [_service(id=service)]


@pytest.mark.parametrize("user", [None, USER, OTHER])
def test_only_admins_verify(
    client: FlaskClient, service: str, user: str | None
) -> None:
    """Only admins can verify services and remove verifications.

    Neither their owners nor other users can.
    """
    status = 401 if user is None else 403
    assert verify(client, user=user).status_code == status
    assert verified(client) == []
    assert verify(client).status_code == 200
    assert unverify(client, user=user).status_code == status
    assert verified(client) == [service]


@pytest.mark.parametrize("user", [None, USER])
def test_public_read(client: FlaskClient, service: str, user: str | None) -> None:
    """Anyone can read verifications, also without an access token."""
    assert verify(client).status_code == 200
    assert verification(client, user=user).status_code == 200
    response = client.get(f"{BASE}/verifications", headers=headers(user))
    assert response.status_code == 200


def test_unverify(client: FlaskClient, service: str) -> None:
    """Admins remove verifications; removing them again returns 404."""
    assert verify(client).status_code == 200
    response = unverify(client)
    assert response.status_code == 200
    assert response.get_json() == service
    assert verification(client).status_code == 404
    assert verified(client) == []
    assert unverify(client).status_code == 404


def test_not_found(client: FlaskClient) -> None:
    """Unknown and unverified services have no verification."""
    for method in ["get", "put", "delete"]:
        response = verification(client, "UNKNWN", method=method, user=ADMIN)
        assert response.status_code == 404
        assert response.content_type == ERROR_MEDIA_TYPE
    assert put_service(client) == 201
    assert verification(client).status_code == 404
    assert unverify(client).status_code == 404


def test_list(client: FlaskClient) -> None:
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
def test_reset_on_change(
    client: FlaskClient, service: str, change: dict[str, Any]
) -> None:
    """Changing the URL (as written) or API type removes the verification."""
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
def test_kept_on_update(
    client: FlaskClient, service: str, change: dict[str, Any]
) -> None:
    """Updating a service with the same URL and API type keeps its verification.

    The verification is served with the URL and API type as updated.
    """
    verified_at = verify(client).get_json()["verified_at"]
    assert put_service(client, _service(**change), user=USER) == 200
    assert verification(client).get_json() == expected(verified_at, _service(**change))
    assert client.get(f"{BASE}/services/{service}").get_json() == _service(
        id=service, **change
    )


def test_removed_with_service(client: FlaskClient, service: str) -> None:
    """Deleting a service deletes its verification.

    A new service with the identifier is not verified.
    """
    assert verify(client).status_code == 200
    response = client.delete(f"{BASE}/services/{service}", headers=headers(USER))
    assert response.status_code == 200
    assert verified(client) == []
    assert put_service(client) == 201
    assert verification(client).status_code == 404


def test_rejected_update_keeps_verification(client: FlaskClient, service: str) -> None:
    """Updates rejected as duplicates keep the verification."""
    assert verify(client).status_code == 200
    other = _service(url="https://other.example.org")
    assert put_service(client, other, service_id="OTHER") == 201
    assert put_service(client, other, user=USER) == 409
    assert verified(client) == [service]


def test_spec(client: FlaskClient) -> None:
    """The GA4GH endpoints and schemas are unchanged.

    Verifications are served by endpoints of their own.
    """
    spec = client.get(f"{BASE}/openapi.json").get_json()
    ga4gh = [
        spec["paths"]["/services"]["get"],
        spec["paths"]["/services/{serviceId}"]["get"],
        spec["components"]["schemas"]["ExternalService"],
    ]
    assert "verif" not in dumps(ga4gh).lower()


def test_logged_values_escaped(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Values from request paths are logged with control characters escaped.

    So they can't forge or hide log lines (line breaks are rejected in
    identifiers, as they can't be stored in policy rules).
    """
    caplog.set_level("INFO")
    service_id = "MY%1BSERV"  # with an escape character
    assert put_service(client, service_id=service_id) == 201
    assert verify(client, service_id=service_id).status_code == 200
    assert "Service 'MY\\x1bSERV' verified by" in caplog.text
    assert "MY\x1bSERV" not in caplog.text


def test_verification_changes_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Verifications, and their removal by admins or updates, are logged.

    The messages name the user who changed them.
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
