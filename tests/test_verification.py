"""Tests for verifications of services, with the app created from its config
(cf. `tests/conftest.py`).
"""

from copy import deepcopy
from datetime import datetime, timezone
from json import dumps

import pytest

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


def verification(client, service_id=SERVICE_ID, method="get", user=None):
    """Send a request to the verification of a service."""
    return client.open(
        f"{BASE}/services/{service_id}/verification",
        method=method,
        headers=headers(user),
    )


def verify(client, service_id=SERVICE_ID, user=ADMIN):
    return verification(client, service_id, method="put", user=user)


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

    response = verification(client)
    assert response.status_code == 200
    assert response.json == {"verified_at": verified_at}
    response = client.get(f"{BASE}/verifications")
    assert response.json == [{"service_id": service, "verified_at": verified_at}]


def test_verify_again(client, service):
    """Verifying a verified service renews its verification."""
    first = verify(client).json["verified_at"]
    second = verify(client)
    assert second.status_code == 200
    assert second.json["verified_at"] >= first
    assert verified(client) == [service]


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
    ],
)
def test_reset_on_change(client, service, change):
    """Changing the URL or API type of a service removes its verification."""
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
        # same URL and API type, in another spelling
        {"url": "HTTPS://TES.example.org:443/ga4gh/tes/v1/"},
        {"type": {**SERVICE["type"], "artifact": " TES "}},
    ],
)
def test_kept_on_update(client, service, change):
    """Updating a service with the same URL and API type keeps its
    verification.
    """
    verified_at = verify(client).json["verified_at"]
    assert put_service(client, _service(**change), user=USER) == 200
    assert verification(client).json == {"verified_at": verified_at}
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
