"""Tests for service types, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`.
"""

from copy import deepcopy
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from pymongo import MongoClient
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
from cloud_registry.utils import get_custom_config
from tests.conftest import ADMIN, USER, make_headers
from tests.mock_data import DB

BASE = "/ga4gh/registry/v1"
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
