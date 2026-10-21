"""Unit tests for endpoint controllers."""

from copy import deepcopy
import inspect
from typing import Any

from flask import Flask
from pymongo.collection import Collection
import pytest

from cloud_registry.exceptions import BadRequest, NotFound
from cloud_registry.ga4gh.registry import server
from cloud_registry.ga4gh.registry.server import (
    deleteService,
    getServiceById,
    getServices,
    getServiceTypes,
    postService,
    putService,
)
from tests.mock_data import (
    MOCK_ID,
    MOCK_PUBLIC_URL,
    MOCK_SERVICE,
    MOCK_TYPE,
)


@pytest.fixture(autouse=True)
def ownership(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record changes of service ownership; the caller is an admin.

    Changes of ownership require access control (cf.
    `tests/test_access_control.py`).
    """
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "is_admin", lambda: True)
    for name in ["add_owner", "remove_permissions"]:
        monkeypatch.setattr(
            server, name, lambda serviceId, name=name: calls.append((name, serviceId))
        )
    return calls


def _insert_services(
    services: Collection[dict[str, Any]], ids: list[str]
) -> list[dict[str, Any]]:
    """Insert copies of the mock service with the given identifiers.

    Args:
        services: Collection of services.
        ids: Identifiers of the services.

    Returns:
        The inserted services, without the database's `_id` fields.
    """
    data = []
    for i in ids:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        services.insert_one(mock_resp)
        del mock_resp["_id"]
        data.append(mock_resp)
    return data


# GET /services
def test_getServices(unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
    """Test for getting a list of all available services."""
    data = _insert_services(services, ["serv1", "serv2", "serv3"])

    with unit_app.app_context():
        res = inspect.unwrap(getServices)()
        assert res == data


# GET /services/{serviceId}
def test_getServiceById(unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
    """Test for getting a service associated with a given identifier."""
    _insert_services(services, [MOCK_ID, "serv2", "serv3"])

    # check whether one service can be retrieved by id
    mock_service = deepcopy(MOCK_SERVICE)
    mock_service["id"] = MOCK_ID
    with unit_app.app_context():
        res = inspect.unwrap(getServiceById)(MOCK_ID)
        assert res == mock_service

    # check whether error is raised if ID does not exist
    with pytest.raises(NotFound), unit_app.app_context():
        res = inspect.unwrap(getServiceById)("serv4")


# GET /services/types
def test_getServiceTypes_duplicates(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Test for listing service types if all services have the same type."""
    _insert_services(services, ["serv1", "serv2", "serv3"])

    with unit_app.app_context():
        res = inspect.unwrap(getServiceTypes)()
        # All written services have same type, we expect list of length 1
        assert res == [MOCK_TYPE]


# GET /services/types
def test_getServiceTypes_distinct(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Test for listing service types if all services have distinct types."""
    ids = ["serv3", "serv1", "serv2"]
    for i in ids:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        mock_resp["type"]["artifact"] = i
        services.insert_one(mock_resp)

    with unit_app.app_context():
        res = inspect.unwrap(getServiceTypes)()
        # All written services have distinct types, sorted by artifact
        assert res == [{**MOCK_TYPE, "artifact": i} for i in sorted(ids)]


# GET /services/types
def test_getServiceTypes_sort_order(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Service types are sorted by group, then artifact, then version."""
    expected = [
        {"group": "a", "artifact": "b", "version": "2"},
        {"group": "a", "artifact": "c", "version": "1"},
        {"group": "a", "artifact": "c", "version": "2"},
        {"group": "b", "artifact": "a", "version": "1"},
    ]
    for i, service_type in enumerate(reversed(expected * 2)):
        services.insert_one({**MOCK_SERVICE, "id": f"serv{i}", "type": service_type})

    with unit_app.app_context():
        assert inspect.unwrap(getServiceTypes)() == expected


# POST /service
def test_postService(
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    ownership: list[tuple[str, str]],
) -> None:
    """Test for registering a service; identifier assigned by implementation."""
    data = deepcopy(MOCK_SERVICE)
    data["id"] = MOCK_ID
    with unit_app.test_request_context("/services", json=data):
        service_id, status, headers = inspect.unwrap(postService)()
    assert status == 201
    assert headers == {"Location": f"{MOCK_PUBLIC_URL}/services/{service_id}"}
    assert ownership == [
        ("remove_permissions", service_id),
        ("add_owner", service_id),
    ]


def test_postService_trailing_slash(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Locations of services posted with a trailing slash have no empty segment.

    I.e., of services posted to the collection URL with a trailing slash.
    """
    with unit_app.test_request_context("/services/", json=deepcopy(MOCK_SERVICE)):
        service_id, _, headers = inspect.unwrap(postService)()
    assert headers == {"Location": f"{MOCK_PUBLIC_URL}/services/{service_id}"}


def test_postService_invalid_payload(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Test for registering a service with an invalid payload.

    The identifier is assigned by the implementation.
    """
    with pytest.raises(BadRequest), unit_app.test_request_context(json=""):
        inspect.unwrap(postService)()


# DELETE /service/{serviceId}
def test_deleteService(
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    ownership: list[tuple[str, str]],
) -> None:
    """Test for deleting a service."""
    _insert_services(services, [MOCK_ID])

    with unit_app.app_context():
        res = inspect.unwrap(deleteService)(serviceId=MOCK_ID)
        assert res == MOCK_ID
    assert ownership == [("remove_permissions", MOCK_ID)] * 2


def test_deleteService_NotFound(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Test for deleting a service if no service has the identifier."""
    services.insert_one(deepcopy(MOCK_SERVICE))

    with unit_app.app_context(), pytest.raises(NotFound):
        inspect.unwrap(deleteService)(serviceId=MOCK_ID)


def test_deleteService_concurrently(
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    ownership: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service deleted concurrently is not found, and its roles are revoked.

    I.e., a service deleted after it was found.
    """
    monkeypatch.setattr(server, "service_exists", lambda serviceId: True)
    with unit_app.app_context(), pytest.raises(NotFound):
        inspect.unwrap(deleteService)(serviceId=MOCK_ID)
    assert ownership == [("remove_permissions", MOCK_ID)]


# PUT /service/{serviceId}
def test_putService(
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    ownership: list[tuple[str, str]],
) -> None:
    """Test for registering a service; identifier provided by client."""
    data = deepcopy(MOCK_SERVICE)
    path = f"/services/{MOCK_ID}"
    with unit_app.test_request_context(path, json=data):
        res = inspect.unwrap(putService)(serviceId=MOCK_ID)
        assert res == (MOCK_ID, 201, {"Location": f"{MOCK_PUBLIC_URL}{path}"})
    # only new services get an owner (and a location)
    with unit_app.test_request_context(path, json=data):
        assert inspect.unwrap(putService)(serviceId=MOCK_ID) == MOCK_ID
    assert ownership == [("remove_permissions", MOCK_ID), ("add_owner", MOCK_ID)]


def test_putService_not_admin(
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    ownership: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only admins can register services with a chosen identifier.

    Others can only replace existing ones.
    """
    monkeypatch.setattr(server, "is_admin", lambda: False)
    path = f"/services/{MOCK_ID}"
    with (
        unit_app.test_request_context(path, json=deepcopy(MOCK_SERVICE)),
        pytest.raises(NotFound),
    ):
        inspect.unwrap(putService)(serviceId=MOCK_ID)
    _insert_services(services, [MOCK_ID])
    with unit_app.test_request_context(path, json=deepcopy(MOCK_SERVICE)):
        assert inspect.unwrap(putService)(serviceId=MOCK_ID) == MOCK_ID
    assert services.count_documents({}) == 1
    assert ownership == []


def test_putService_invalid_payload(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Test for registering a service with an invalid payload.

    The identifier is provided by the client.
    """
    with pytest.raises(BadRequest), unit_app.test_request_context(json=""):
        inspect.unwrap(putService)(serviceId=MOCK_ID)
