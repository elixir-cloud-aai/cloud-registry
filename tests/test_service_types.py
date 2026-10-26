"""Tests for service types, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Literal

from connexion import App
from flask.testing import FlaskClient
from pymongo import MongoClient
import pytest
from werkzeug.test import TestResponse
import yaml

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry import server, service_types
from cloud_registry.ga4gh.registry.service import PENDING_SERVICE, RegisterService
from cloud_registry.utils import get_custom_config
from tests.conftest import ADMIN, CONFIG_OVERLAY, USER, make_headers
from tests.mock_data import DB

BASE = "/ga4gh/registry/v1"
# Another user than `USER`
OTHER = "bob@example.org"
TYPES = f"{BASE}/services/types"
TES = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
WES = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": TES,
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}


def headers(user: str | None = USER) -> dict[str, str]:
    """Get request headers with an access token for a user.

    Args:
        user: User identifier; no token if `None`.

    Returns:
        Request headers.
    """
    return {} if user is None else make_headers(user)


def _service(service_type: dict[str, Any] = TES, **kwargs: Any) -> dict[str, Any]:
    """Get a copy of the test service, with another type or fields.

    Args:
        service_type: Type of the service.
        kwargs: Further fields to set.

    Returns:
        The service.
    """
    service = deepcopy(SERVICE)
    service.update(type=dict(service_type), **kwargs)
    return service


def add_type(
    client: FlaskClient, service_type: dict[str, Any], user: str | None = USER
) -> TestResponse:
    """Register a service type (`POST /services/types`).

    Args:
        client: Test client of the app.
        service_type: Service type.
        user: User who sends the request; anonymous if `None`.

    Returns:
        Response.
    """
    return client.post(TYPES, json=service_type, headers=headers(user))


def remove_type(
    client: FlaskClient, service_type: dict[str, Any], user: str | None = ADMIN
) -> TestResponse:
    """Remove a service type (`DELETE /services/types?group=...`).

    Args:
        client: Test client of the app.
        service_type: Service type.
        user: User who sends the request; anonymous if `None`.

    Returns:
        Response.
    """
    return client.delete(TYPES, query_string=service_type, headers=headers(user))


def listed(client: FlaskClient) -> list[dict[str, str]]:
    """List the service types (`GET /services/types`).

    Args:
        client: Test client of the app.

    Returns:
        Listed types.
    """
    response = client.get(TYPES)
    assert response.status_code == 200
    types: list[dict[str, str]] = response.get_json()
    return types


def register(
    client: FlaskClient, service: dict[str, Any], user: str = USER
) -> TestResponse:
    """Register a service (`POST /services`).

    Args:
        client: Test client of the app.
        service: Service.
        user: User who sends the request.

    Returns:
        Response.
    """
    return client.post(f"{BASE}/services", json=service, headers=headers(user))


def may_add(client: FlaskClient, user: str) -> bool:
    """Get whether a user may add service types, as `GET /me` tells.

    Args:
        client: Test client of the app.
        user: User.

    Returns:
        `may_add_service_types` of the user.
    """
    response = client.get(f"{BASE}/me", headers=headers(user))
    assert response.status_code == 200, response.get_json()
    value: bool = response.get_json()["may_add_service_types"]
    return value


@pytest.fixture
def moderated(app: App) -> None:
    """Let admins approve the service types of other users."""
    with app.app.app_context():
        get_custom_config().access_control.service_types = "moderated"


def approvals(client: FlaskClient, user: str) -> dict[str, list[Any]]:
    """List pending types and services (`GET /approvals`).

    Args:
        client: Test client of the app.
        user: User who sends the request.

    Returns:
        Pending types (by artifact) and services (by identifier).
    """
    response = client.get(f"{BASE}/approvals", headers=headers(user))
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    return {
        "service_types": [item["artifact"] for item in body["service_types"]],
        "services": [item["id"] for item in body["services"]],
    }


def approve(
    client: FlaskClient, service_type: dict[str, Any], user: str | None = ADMIN
) -> TestResponse:
    """Approve a service type (`PUT /services/types/approval`).

    Args:
        client: Test client of the app.
        service_type: Service type.
        user: User who sends the request; anonymous if `None`.

    Returns:
        Response.
    """
    return client.put(f"{TYPES}/approval", json=service_type, headers=headers(user))


def pending(client: FlaskClient, user: str) -> bool:
    """Get whether a user's new types are pending, as `GET /me` tells.

    Args:
        client: Test client of the app.
        user: User.

    Returns:
        `service_types_pending` of the user.
    """
    response = client.get(f"{BASE}/me", headers=headers(user))
    assert response.status_code == 200, response.get_json()
    value: bool = response.get_json()["service_types_pending"]
    return value


@pytest.fixture
def controlled(app: App) -> None:
    """Let only admins add service types."""
    with app.app.app_context():
        get_custom_config().access_control.service_types = "controlled"


def _created_by(mongo_client: MongoClient[dict[str, Any]]) -> dict[str, Any]:
    """Get who added the stored service types.

    Args:
        mongo_client: MongoDB client of the app.

    Returns:
        User who added each type, by artifact.
    """
    return {
        doc["artifact"]: doc["created_by"]
        for doc in mongo_client[DB]["service_types"].find()
    }


def test_add_type(
    client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Authenticated users add types, which are listed, sorted.

    Who added a type is stored with it, but not served.
    """
    assert listed(client) == []
    response = add_type(client, WES)
    assert response.status_code == 201, response.get_json()
    assert response.get_json() == WES
    assert add_type(client, TES).status_code == 201
    assert listed(client) == [TES, WES]
    assert may_add(client, USER)
    assert _created_by(mongo_client) == {"tes": USER, "wes": USER}


def test_add_type_again(client: FlaskClient) -> None:
    """Types are registered once, as written."""
    assert add_type(client, TES).status_code == 201
    response = add_type(client, TES)
    assert response.status_code == 409
    assert response.get_json()["detail"] == "The service type is registered."
    assert add_type(client, {**TES, "artifact": "TES"}).status_code == 201
    assert len(listed(client)) == 2


def test_add_type_anonymous(client: FlaskClient) -> None:
    """Adding types requires authentication."""
    assert add_type(client, TES, user=None).status_code == 401
    assert listed(client) == []


@pytest.mark.parametrize(
    "body",
    [
        {"group": "org.ga4gh", "artifact": "tes"},
        {**TES, "extra": "x"},
        {**TES, "version": "x" * 257},
        {**TES, "version": 1},
    ],
)
def test_add_type_invalid(client: FlaskClient, body: dict[str, Any]) -> None:
    """Types must have the three strings, and nothing else."""
    assert add_type(client, body).status_code == 400
    assert listed(client) == []


def test_add_type_surrogates(client: FlaskClient) -> None:
    """Strings that can't be stored are rejected."""
    response = client.post(
        TYPES,
        data='{"group": "org.ga4gh", "artifact": "\\ud800", "version": "1"}',
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "'artifact'" in response.get_json()["detail"]


def test_type_of_service_added(
    client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """New types of registered and updated services are stored."""
    response = register(client, _service())
    assert response.status_code == 201, response.get_json()
    assert listed(client) == [TES]
    response = client.put(
        f"{BASE}/services/{response.get_json()}",
        json=_service(WES),
        headers=headers(),
    )
    assert response.status_code == 200, response.get_json()
    # the former type is kept
    assert listed(client) == [TES, WES]
    assert _created_by(mongo_client) == {"tes": USER, "wes": USER}


def test_remove_type(client: FlaskClient) -> None:
    """Admins remove types that no service has."""
    assert add_type(client, TES).status_code == 201
    response = remove_type(client, TES)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == TES
    assert listed(client) == []
    assert remove_type(client, TES).status_code == 404


def test_remove_type_not_admin(client: FlaskClient) -> None:
    """Only admins remove types."""
    assert add_type(client, TES).status_code == 201
    assert remove_type(client, TES, user=USER).status_code == 403
    assert remove_type(client, TES, user=None).status_code == 401
    assert listed(client) == [TES]


def test_remove_type_in_use(client: FlaskClient) -> None:
    """Types of registered services can't be removed."""
    service_id = register(client, _service()).get_json()
    response = remove_type(client, TES)
    assert response.status_code == 409
    assert response.get_json()["detail"] == (
        "Service type is in use. If you really want to revoke it, edit or "
        "delete all services that depend on it."
    )
    assert listed(client) == [TES]
    assert (
        client.delete(f"{BASE}/services/{service_id}", headers=headers()).status_code
        == 200
    )
    assert remove_type(client, TES).status_code == 200


def test_controlled_add_type(client: FlaskClient, controlled: None) -> None:
    """In mode `controlled`, only admins add types."""
    response = add_type(client, TES)
    assert response.status_code == 403
    assert response.get_json()["detail"] == "Only admins may add service types."
    assert not may_add(client, USER)
    assert add_type(client, TES, user=ADMIN).status_code == 201
    assert may_add(client, ADMIN)
    assert listed(client) == [TES]


def test_controlled_services(client: FlaskClient, controlled: None) -> None:
    """In mode `controlled`, services of users must have a registered type.

    Services of admins may have new types, which are stored.
    """
    response = register(client, _service())
    assert response.status_code == 400
    detail = response.get_json()["detail"]
    assert '{"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}' in detail
    assert "is not registered" in detail
    assert client.get(f"{BASE}/services").get_json() == []
    assert register(client, _service(), user=ADMIN).status_code == 201
    assert listed(client) == [TES]
    response = register(client, _service(url="https://tes2.example.org"))
    assert response.status_code == 201, response.get_json()
    # nor can users update services to new types
    response = client.put(
        f"{BASE}/services/{response.get_json()}",
        json=_service(WES, url="https://tes2.example.org"),
        headers=headers(),
    )
    assert response.status_code == 400
    assert listed(client) == [TES]


def test_controlled_type_removed_meanwhile(
    client: FlaskClient,
    controlled: None,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registration in progress stores a type removed meanwhile.

    In mode `controlled`, the type is then stored without the user.
    """
    assert add_type(client, TES, user=ADMIN).status_code == 201
    check = server.check_service_type

    def check_and_remove(value: object) -> None:
        """Check the type, then let an admin remove it.

        Args:
            value: Type of the service.
        """
        check(value)
        mongo_client[DB]["service_types"].delete_many({})

    monkeypatch.setattr(server, "check_service_type", check_and_remove)
    assert register(client, _service()).status_code == 201
    assert listed(client) == [TES]
    assert _created_by(mongo_client) == {"tes": None}


def test_types_added_on_startup(
    app_env: None, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Types of services registered before types were stored are added."""
    mongo_client[DB]["services"].insert_many(
        [
            {**SERVICE, "id": "A"},
            {**SERVICE, "id": "B", "url": "https://other.example.org"},
            {**_service(WES), "id": "C", "url": "https://wes.example.org"},
            # invalid types are skipped
            {**_service({**WES, "version": 1}), "id": "D", "url": "https://d.org"},
        ]
    )
    client = create_app().app.test_client()
    assert listed(client) == [TES, WES]
    # idempotent
    client = create_app().app.test_client()
    assert listed(client) == [TES, WES]


def test_moderated_add_type(client: FlaskClient, moderated: None) -> None:
    """In mode `moderated`, types of users are pending until approved.

    Only admins and the users who added them see them meanwhile, and only
    admins approve them.
    """
    assert may_add(client, USER)
    assert pending(client, USER)
    assert not pending(client, ADMIN)
    response = add_type(client, TES)
    assert response.status_code == 201, response.get_json()
    assert listed(client) == []
    assert approvals(client, USER) == {"service_types": ["tes"], "services": []}
    assert approvals(client, OTHER) == {"service_types": [], "services": []}
    assert approvals(client, ADMIN) == {"service_types": ["tes"], "services": []}
    # registered, though pending
    assert add_type(client, TES, user=OTHER).status_code == 409
    assert approve(client, TES, user=USER).status_code == 403
    assert approve(client, TES, user=None).status_code == 401
    response = approve(client, TES)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == TES
    assert listed(client) == [TES]
    assert approvals(client, ADMIN) == {"service_types": [], "services": []}
    # approving again changes nothing
    assert approve(client, TES).get_json() == TES
    assert listed(client) == [TES]


def test_moderated_admin_types(client: FlaskClient, moderated: None) -> None:
    """In mode `moderated`, types that admins add are approved at once."""
    assert add_type(client, TES, user=ADMIN).status_code == 201
    response = register(client, _service(WES), user=ADMIN)
    assert response.status_code == 201, response.get_json()
    assert listed(client) == [TES, WES]
    assert len(client.get(f"{BASE}/services").get_json()) == 1
    assert approvals(client, ADMIN) == {"service_types": [], "services": []}


def test_approve_type_not_found(client: FlaskClient) -> None:
    """Types that aren't registered can't be approved."""
    assert approve(client, TES).status_code == 404


def test_approve_type_surrogates(client: FlaskClient) -> None:
    """Strings that can't be stored are rejected."""
    response = client.put(
        f"{TYPES}/approval",
        data='{"group": "org.ga4gh", "artifact": "\\ud800", "version": "1"}',
        headers={**headers(ADMIN), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "'artifact'" in response.get_json()["detail"]


def test_moderated_services(client: FlaskClient, moderated: None) -> None:
    """In mode `moderated`, services with a pending type are pending.

    Reads without login don't list them; only their owners and admins see
    them, and only admins update them until the type is approved. The detail
    of duplicates doesn't name them.
    """
    service_id = register(client, _service()).get_json()
    other_id = register(client, _service(url="https://tes2.example.org"), OTHER)
    other_id = other_id.get_json()
    assert client.get(f"{BASE}/services").get_json() == []
    assert client.get(f"{BASE}/services/{service_id}").status_code == 404
    assert approvals(client, USER) == {
        "service_types": ["tes"],
        "services": [service_id],
    }
    # services of other users with the pending type are pending too
    assert approvals(client, OTHER) == {"service_types": [], "services": [other_id]}
    assert approvals(client, ADMIN) == {
        "service_types": ["tes"],
        "services": sorted([service_id, other_id]),
    }
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(name="New"), headers=headers()
    )
    assert response.status_code == 409
    assert "pending" in response.get_json()["detail"]
    response = client.put(
        f"{BASE}/services/{service_id}",
        json=_service(name="By admin"),
        headers=headers(ADMIN),
    )
    assert response.status_code == 200, response.get_json()
    # duplicates of pending services are not named
    response = register(client, _service(), OTHER)
    assert response.status_code == 409
    assert "serviceId" not in response.get_json()
    assert approve(client, TES).status_code == 200
    services = client.get(f"{BASE}/services").get_json()
    assert sorted(service["id"] for service in services) == sorted(
        [service_id, other_id]
    )
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(name="New"), headers=headers()
    )
    assert response.status_code == 200, response.get_json()


def test_moderated_delete_pending(client: FlaskClient, moderated: None) -> None:
    """Owners delete pending services; admins remove pending types."""
    service_id = register(client, _service()).get_json()
    response = client.delete(f"{BASE}/services/{service_id}", headers=headers())
    assert response.status_code == 200
    assert approvals(client, USER) == {"service_types": ["tes"], "services": []}
    assert remove_type(client, TES, user=USER).status_code == 403
    assert remove_type(client, TES).status_code == 200
    assert approvals(client, ADMIN) == {"service_types": [], "services": []}


def test_moderated_login_for_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """If reads require login, owners and admins see pending services in reads.

    The users who added pending types see them, too.
    """
    overlay = {
        "api": {
            "specs": {"registry": {"patches": ["api/patches/login-for-reads.yaml"]}}
        },
        "custom": {"access_control": {"service_types": "moderated"}},
    }
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv(
        "FOCA_CONFIG_OVERLAY", os.pathsep.join([str(CONFIG_OVERLAY), str(path)])
    )
    client = create_app().app.test_client()
    service_id = register(client, _service()).get_json()
    for user, visible in [(USER, True), (ADMIN, True), (OTHER, False)]:
        services = client.get(f"{BASE}/services", headers=headers(user))
        assert len(services.get_json()) == visible, user
        response = client.get(f"{BASE}/services/{service_id}", headers=headers(user))
        assert response.status_code == (200 if visible else 404), user
        types = client.get(TYPES, headers=headers(user)).get_json()
        assert types == ([TES] if visible else []), user


def test_moderated_verifications(client: FlaskClient, moderated: None) -> None:
    """Verifications of pending services are hidden like the services."""
    service_id = register(client, _service()).get_json()
    path = f"{BASE}/services/{service_id}/verification"
    assert client.put(path, headers=headers(ADMIN)).status_code == 200
    assert client.get(path).status_code == 404
    assert client.get(f"{BASE}/verifications").get_json() == []
    assert approve(client, TES).status_code == 200
    assert client.get(path).status_code == 200
    verifications = client.get(f"{BASE}/verifications").get_json()
    assert [item["service_id"] for item in verifications] == [service_id]


def test_moderated_type_changed(client: FlaskClient, moderated: None) -> None:
    """Services updated to a pending type are pending, then can't be updated."""
    assert add_type(client, TES, user=ADMIN).status_code == 201
    service_id = register(client, _service()).get_json()
    assert approvals(client, USER)["services"] == []
    path = f"{BASE}/services/{service_id}"
    response = client.put(path, json=_service(WES), headers=headers())
    assert response.status_code == 200, response.get_json()
    assert approvals(client, USER) == {
        "service_types": ["wes"],
        "services": [service_id],
    }
    response = client.put(path, json=_service(WES, name="X"), headers=headers())
    assert response.status_code == 409
    # back to an approved type
    response = client.put(path, json=_service(TES), headers=headers())
    assert response.status_code == 200, response.get_json()
    assert approvals(client, USER)["services"] == []


def test_moderated_duplicate_named(client: FlaskClient, moderated: None) -> None:
    """Duplicates of pending services are named to users who see them."""
    service_id = register(client, _service()).get_json()
    response = register(client, _service(), ADMIN)
    assert response.status_code == 409
    assert response.get_json()["serviceId"] == service_id


def test_moderated_then_open(app: App, client: FlaskClient, moderated: None) -> None:
    """Pending types stay pending if the mode changes; new types aren't."""
    service_id = register(client, _service()).get_json()
    with app.app.app_context():
        get_custom_config().access_control.service_types = "open"
    assert not pending(client, USER)
    assert add_type(client, WES).status_code == 201
    assert listed(client) == [WES]
    assert approvals(client, USER) == {
        "service_types": ["tes"],
        "services": [service_id],
    }


def test_moderated_type_stored_first(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """New types are only stored for valid services.

    Registrations that fail validation store no pending type; strings that
    can't be stored are rejected.
    """
    response = register(client, _service(url="https://[::1"))
    assert response.status_code == 400
    assert approvals(client, USER)["service_types"] == []
    assert mongo_client[DB]["service_types"].count_documents({}) == 0
    service = _service()
    service["type"]["artifact"] = "\ud800"
    response = client.post(
        f"{BASE}/services",
        data=json.dumps(service),
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert mongo_client[DB]["service_types"].count_documents({}) == 0


def test_moderated_update_surrogates(client: FlaskClient, moderated: None) -> None:
    """Updates of pending services with strings that can't be stored fail."""
    service_id = register(client, _service()).get_json()
    service = _service()
    service["type"]["artifact"] = "\ud800"
    response = client.put(
        f"{BASE}/services/{service_id}",
        data=json.dumps(service),
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert "'type.artifact'" in response.get_json()["detail"]


def _flags(mongo_client: MongoClient[dict[str, Any]]) -> dict[str, bool]:
    """Get the stored pending flags of the services.

    Args:
        mongo_client: MongoDB client of the app.

    Returns:
        Whether each service is flagged as pending, by identifier.
    """
    return {
        doc["id"]: doc.get(PENDING_SERVICE, False)
        for doc in mongo_client[DB]["services"].find()
    }


@pytest.fixture
def unused_limit(app: App) -> None:
    """Let users other than admins add at most two unused types."""
    with app.app.app_context():
        get_custom_config().access_control.max_unused_service_types = 2


def test_many_pending_types(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Reads don't grow with the number of pending types.

    A filter by all pending types, as before, would exceed MongoDB's limit of
    16 MB (60000 types of over 300 bytes each).
    """
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("MongoDB's limit; slow with mongomock")
    long = "x" * 100
    mongo_client[DB]["service_types"].insert_many(
        {"group": long, "artifact": f"{long}{index}", "version": long, "pending": True}
        for index in range(60000)
    )
    approved_id = register(client, _service(), ADMIN).get_json()
    pending_id = register(client, _service(WES, url="https://wes.example.org"))
    pending_id = pending_id.get_json()
    ids = {service["id"] for service in client.get(f"{BASE}/services").get_json()}
    assert ids == {approved_id}
    assert client.get(f"{BASE}/services/{pending_id}").status_code == 404
    assert client.get(f"{BASE}/verifications").status_code == 200
    assert approvals(client, USER) == {
        "service_types": ["wes"],
        "services": [pending_id],
    }


def test_pending_flag_not_served(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Pending services are flagged in the database, but it isn't served.

    Approvals clear the flag; changes to an approved type clear it too.
    """
    service_id = register(client, _service()).get_json()
    other_id = register(client, _service(WES, url="https://wes.example.org"))
    other_id = other_id.get_json()
    assert _flags(mongo_client) == {service_id: True, other_id: True}
    response = client.get(f"{BASE}/approvals", headers=headers())
    assert response.status_code == 200
    assert all(PENDING_SERVICE not in item for item in response.get_json()["services"])
    assert approve(client, TES).status_code == 200
    assert _flags(mongo_client) == {service_id: False, other_id: True}
    response = client.put(
        f"{BASE}/services/{other_id}",
        json=_service(url="https://wes.example.org"),
        headers=headers(ADMIN),
    )
    assert response.status_code == 200, response.get_json()
    assert _flags(mongo_client) == {service_id: False, other_id: False}
    # and back to the pending type
    response = client.put(
        f"{BASE}/services/{other_id}",
        json=_service(WES, url="https://wes.example.org"),
        headers=headers(ADMIN),
    )
    assert response.status_code == 200, response.get_json()
    assert _flags(mongo_client) == {service_id: False, other_id: True}


def test_approved_during_registration(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type approved during a registration doesn't leave it pending."""
    register_metadata = RegisterService.register_metadata

    def approve_and_register(self: RegisterService, retries: int = 9) -> None:
        """Let an admin approve the type, then register the service.

        Args:
            self: The registration.
            retries: Cf. `RegisterService.register_metadata()`.
        """
        assert self.pending
        assert approve(client, TES).status_code == 200
        register_metadata(self, retries=retries)

    monkeypatch.setattr(RegisterService, "register_metadata", approve_and_register)
    service_id = register(client, _service()).get_json()
    assert _flags(mongo_client) == {service_id: False}
    assert len(client.get(f"{BASE}/services").get_json()) == 1


def test_approve_again_clears_flags(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
) -> None:
    """Approving an approved type clears flags left on its services."""
    service_id = register(client, _service()).get_json()
    assert approve(client, TES).status_code == 200
    mongo_client[DB]["services"].update_one(
        {"id": service_id}, {"$set": {PENDING_SERVICE: True}}
    )
    assert approve(client, TES).status_code == 200
    assert _flags(mongo_client) == {service_id: False}


def test_pending_flags_synced_on_startup(
    app_env: None, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Pending flags are set and cleared by the types on startup."""
    mongo_client[DB]["service_types"].insert_many([{**TES}, {**WES, "pending": True}])
    mongo_client[DB]["services"].insert_many(
        [
            {**SERVICE, "id": "A", PENDING_SERVICE: True},
            {**_service(WES), "id": "B", "url": "https://wes.example.org"},
            {**_service(WES), "id": "C", "url": "https://c.org", PENDING_SERVICE: True},
            # invalid types are skipped
            {**_service({**WES, "version": 1}), "id": "D", "url": "https://d.org"},
        ]
    )
    create_app()
    assert _flags(mongo_client) == {"A": False, "B": True, "C": True, "D": False}
    # idempotent
    create_app()
    assert _flags(mongo_client) == {"A": False, "B": True, "C": True, "D": False}


@pytest.mark.parametrize("mode", ["open", "moderated"])
def test_unused_types_limit(
    app: App,
    client: FlaskClient,
    unused_limit: None,
    mode: Literal["open", "moderated"],
) -> None:
    """Users other than admins add a limited number of unused types.

    Added through `POST /services/types` or services, in every mode; types
    that services have don't count, nor do those of other users.
    """
    with app.app.app_context():
        get_custom_config().access_control.service_types = mode
    assert add_type(client, TES).status_code == 201
    assert add_type(client, WES).status_code == 201
    third = {**TES, "artifact": "drs"}
    response = add_type(client, third)
    assert response.status_code == 409
    assert "2 service types that no service has" in response.get_json()["detail"]
    response = register(client, _service(third))
    assert response.status_code == 409
    assert "2 service types" in response.get_json()["detail"]
    # types that exist can be used
    assert register(client, _service()).status_code == 201
    assert add_type(client, third).status_code == 201
    assert add_type(client, {**TES, "artifact": "trs"}).status_code == 409
    # other users and admins have their own limits, or none
    assert add_type(client, {**TES, "artifact": "trs"}, user=OTHER).status_code == 201
    for index in range(3):
        response = add_type(client, {**TES, "version": str(index)}, user=ADMIN)
        assert response.status_code == 201


def test_unused_types_unlimited(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Without a limit, users add any number of unused types."""
    with app.app.app_context():
        get_custom_config().access_control.max_unused_service_types = None
    for index in range(12):
        assert add_type(client, {**TES, "version": str(index)}).status_code == 201
    assert mongo_client[DB]["service_types"].count_documents({}) == 12


def test_unused_types_none(app: App, client: FlaskClient) -> None:
    """With a limit of zero, only admins add types."""
    with app.app.app_context():
        get_custom_config().access_control.max_unused_service_types = 0
    assert add_type(client, TES).status_code == 409
    assert register(client, _service()).status_code == 409
    assert add_type(client, TES, user=ADMIN).status_code == 201
    assert register(client, _service()).status_code == 201


def test_add_type_concurrently(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A type added by a concurrent request is registered (`409`)."""
    assert add_type(client, TES).status_code == 201
    monkeypatch.setattr(service_types, "service_type_exists", lambda _: False)
    response = add_type(client, TES, user=OTHER)
    assert response.status_code == 409
    assert response.get_json()["detail"] == "The service type is registered."


def test_unused_types_limit_changes(client: FlaskClient, unused_limit: None) -> None:
    """Changes of services to new types count, and leave old types unused.

    Types become unused again when their services are deleted.
    """
    service_id = register(client, _service()).get_json()
    assert add_type(client, WES).status_code == 201
    assert add_type(client, {**TES, "artifact": "trs"}).status_code == 201
    response = client.put(
        f"{BASE}/services/{service_id}",
        json=_service({**TES, "artifact": "drs"}),
        headers=headers(),
    )
    assert response.status_code == 409
    assert "2 service types" in response.get_json()["detail"]
    # changing to an existing type leaves the old one unused
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(WES), headers=headers()
    )
    assert response.status_code == 200, response.get_json()
    assert add_type(client, {**TES, "artifact": "drs"}).status_code == 409
    # with one unused type left, deleting the service leaves its type unused
    assert remove_type(client, TES).status_code == 200
    assert remove_type(client, {**TES, "artifact": "trs"}).status_code == 200
    assert add_type(client, {**TES, "artifact": "drs"}).status_code == 201
    response = client.delete(f"{BASE}/services/{service_id}", headers=headers())
    assert response.status_code == 200
    assert add_type(client, {**TES, "artifact": "trs"}).status_code == 409


def test_indexes(app: App, mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Pending services and types, and types by user, are indexed."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("indexes of MongoDB")
    services = mongo_client[DB]["services"].index_information()
    types = mongo_client[DB]["service_types"].index_information()
    keys = [index["key"] for index in [*services.values(), *types.values()]]
    assert [(PENDING_SERVICE, 1)] in keys
    assert [("pending", 1)] in keys
    assert [("created_by", 1)] in keys


def test_concurrent_type_change(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change to a pending type meanwhile isn't cleared by an earlier one."""
    assert add_type(client, WES, user=ADMIN).status_code == 201
    service_id = register(client, _service(WES)).get_json()
    sync = server.sync_pending_flag
    calls: list[str] = []

    def change_then_sync(serviceId: str, service_type: Any) -> None:
        """Let the owner change the type to a new one, then sync.

        Args:
            serviceId: Service identifier.
            service_type: Type of the service.
        """
        if not calls:
            calls.append(serviceId)
            response = client.put(
                f"{BASE}/services/{serviceId}", json=_service(), headers=headers()
            )
            assert response.status_code == 200, response.get_json()
        sync(serviceId, service_type)

    monkeypatch.setattr(server, "sync_pending_flag", change_then_sync)
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(WES), headers=headers()
    )
    assert response.status_code == 200, response.get_json()
    assert _flags(mongo_client) == {service_id: True}
    assert client.get(f"{BASE}/services").get_json() == []


def test_approved_during_sync(
    client: FlaskClient,
    moderated: None,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A type approved while flags are set doesn't leave services pending."""
    check = service_types.is_pending
    calls: list[object] = []

    def approve_after_check(service_type: Any) -> bool:
        """Check the type; on the first check of the sync, approve it.

        Args:
            service_type: Service type.

        Returns:
            Whether the type was pending when checked.
        """
        pending = check(service_type)
        calls.append(service_type)
        if len(calls) == 1:
            assert approve(client, TES).status_code == 200
        return pending

    service_id = register(client, _service()).get_json()
    monkeypatch.setattr(service_types, "is_pending", approve_after_check)
    with client.application.test_request_context():
        service_types.sync_pending_flag(service_id, TES)
    assert _flags(mongo_client) == {service_id: False}


def test_unused_types_batches(
    client: FlaskClient,
    moderated: None,
    unused_limit: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unused types are counted in batches; approved ones count too."""
    monkeypatch.setattr(service_types, "UNUSED_TYPES_BATCH", 2)
    for artifact in ["a", "b", "c"]:
        service = _service({**TES, "artifact": artifact}, url=f"https://{artifact}.org")
        assert register(client, service).status_code == 201
    assert add_type(client, TES).status_code == 201
    assert approve(client, TES).status_code == 200
    assert add_type(client, WES).status_code == 201
    assert add_type(client, {**TES, "artifact": "drs"}).status_code == 409
