"""Unit tests for the helpers and controllers of service types."""

from typing import Any, NoReturn

from flask import Flask, Response
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError
import pytest

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.service_types import (
    CREATED_BY,
    check_service_type,
    deleteServiceType,
    postServiceType,
    store_service_type,
    type_document,
)
from tests.mock_data import MOCK_SERVICE, MOCK_TYPE


@pytest.mark.parametrize(
    "value",
    [
        None,
        "org.ga4gh",
        [],
        {"group": "a", "artifact": "b"},
        {**MOCK_TYPE, "version": 1},
    ],
)
def test_type_document_invalid(value: object) -> None:
    """Types that aren't objects with the three strings have no stored form."""
    assert type_document(value) is None


def test_type_document_extra_fields() -> None:
    """Further fields of types are ignored."""
    assert type_document({**MOCK_TYPE, "extra": "x"}) == MOCK_TYPE


def test_check_service_type_invalid(unit_app: Flask) -> None:
    """Invalid types are left to the registration, which rejects them."""
    with unit_app.app_context():
        check_service_type({"group": 1})


def test_store_service_type(
    unit_app: Flask, service_types: Collection[dict[str, Any]]
) -> None:
    """Types are stored once, with the user who added them first."""
    with unit_app.app_context():
        assert store_service_type(MOCK_TYPE, created_by="alice")
        assert not store_service_type(MOCK_TYPE, created_by="bob")
    stored = service_types.find_one({}, {"_id": False})
    assert stored == {**MOCK_TYPE, CREATED_BY: "alice"}


def test_store_service_type_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    unit_app: Flask,
    service_types: Collection[dict[str, Any]],
) -> None:
    """Types stored concurrently (a duplicate key on upsert) are not added."""

    def duplicate(*args: Any, **kwargs: Any) -> NoReturn:
        """Fail as for a type inserted concurrently."""
        raise DuplicateKeyError("duplicate")

    with unit_app.app_context():
        monkeypatch.setattr(service_types, "update_one", duplicate)
        assert not store_service_type(MOCK_TYPE)


def test_post_type_invalid(unit_app: Flask) -> None:
    """Types without the three strings are rejected (the schema requires them)."""
    with unit_app.app_context():
        response = postServiceType({"group": "a", "artifact": "b"})
    assert isinstance(response, Response)
    assert response.status_code == 400


def test_delete_type_not_stored(unit_app: Flask) -> None:
    """Types that are not stored (e.g., removed concurrently) are not found."""
    with unit_app.app_context(), pytest.raises(NotFound):
        deleteServiceType(**MOCK_TYPE)


def test_delete_type_in_use_not_stored(
    unit_app: Flask, services: Collection[dict[str, Any]]
) -> None:
    """Types that services have can't be removed, also if they aren't stored.

    E.g., of services registered by an older version of the app during an
    upgrade.
    """
    services.insert_one({**MOCK_SERVICE, "id": "A", "type": MOCK_TYPE})
    with unit_app.app_context():
        response = deleteServiceType(**MOCK_TYPE)
    assert isinstance(response, Response)
    assert response.status_code == 409


def test_delete_type_used_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    unit_app: Flask,
    services: Collection[dict[str, Any]],
    service_types: Collection[dict[str, Any]],
) -> None:
    """Types that a service gets while they are removed are stored again.

    With the user who added them.
    """
    service_types.insert_one({**MOCK_TYPE, CREATED_BY: "alice"})
    find_one_and_delete = service_types.find_one_and_delete

    def register_meanwhile(*args: Any, **kwargs: Any) -> Any:
        """Remove the type, while a service with the type is registered."""
        result = find_one_and_delete(*args, **kwargs)
        services.insert_one({**MOCK_SERVICE, "id": "A"})
        return result

    with unit_app.app_context():
        monkeypatch.setattr(service_types, "find_one_and_delete", register_meanwhile)
        response = deleteServiceType(**MOCK_TYPE)
    assert isinstance(response, Response)
    assert response.status_code == 409
    stored = service_types.find_one({}, {"_id": False})
    assert stored == {**MOCK_TYPE, CREATED_BY: "alice"}
