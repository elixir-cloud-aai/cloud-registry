"""Tests for the limit of services per user, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`; the helpers for
services and roles are those of `tests/test_access_control.py`. The tests of
concurrent registrations need a MongoDB (`MONGODB_TEST_URI`), as mongomock
doesn't make writes atomic across threads.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from itertools import count
import logging
import os
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca import get_custom_config
from foca.security.access_control import remove_resource_role
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry import server, service_limits
from cloud_registry.ga4gh.registry.service import CREATED_BY
from cloud_registry.ga4gh.registry.service_limits import (
    COUNT,
    SERVICE_COUNTS,
    add_service_creators,
    reconcile_service_counts,
    release_service,
    reserve_service,
    services_path_on_startup,
)
from cloud_registry.service_models.custom_config import CustomConfig
from tests.conftest import ADMIN, COLLECTION_CLASS, TEAM_GROUP
from tests.mock_data import DB
from tests.test_access_control import (
    ALICE,
    BASE,
    BOB,
    grant,
    grant_group,
    headers,
    register,
)
from tests.test_invitations import accept, token_of

# Numbers for the URLs of services registered here, which must be unique
URLS = count()


def _service() -> dict[str, Any]:
    """Get a test service with a new URL.

    Returns:
        The service, without identifier.
    """
    return {
        "name": "My TES",
        "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
        "organization": {"name": "My organization", "url": "https://example.org"},
        "version": "1.0.0",
        "url": f"https://limit{next(URLS)}.example.org",
    }


def _post(
    client: FlaskClient, user: str = ALICE, admin_mode: bool = True
) -> TestResponse:
    """Register a test service with a new URL (`POST /services`).

    Args:
        client: Test client of the app.
        user: User who registers it.
        admin_mode: Whether the request asks for admin mode.

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/services", json=_service(), headers=headers(user, admin_mode)
    )


def _set_limit(app: App, limit: int | None) -> None:
    """Set the limit of services per user.

    Args:
        app: The app.
        limit: Limit (`custom.access_control.max_services_per_user`).
    """
    with app.app.app_context():
        get_custom_config(CustomConfig).access_control.max_services_per_user = limit


def _counts(mongo_client: MongoClient[dict[str, Any]]) -> dict[str, int]:
    """Get the counters of services per user.

    Args:
        mongo_client: MongoDB client of the app.

    Returns:
        Number of services by user.
    """
    return {
        counter["_id"]: counter[COUNT]
        for counter in mongo_client[DB][SERVICE_COUNTS].find({})
    }


def _creator(mongo_client: MongoClient[dict[str, Any]], service_id: str) -> Any:
    """Get the user a stored service counts for.

    Args:
        mongo_client: MongoDB client of the app.
        service_id: Service identifier.

    Returns:
        Value of the internal field; `KeyError` if it is missing.
    """
    stored = mongo_client[DB]["services"].find_one({"id": service_id})
    assert stored is not None
    return stored.get(CREATED_BY, KeyError)


def _limited(response: TestResponse) -> bool:
    """Check whether a registration was refused for the limit.

    Args:
        response: Response of `POST /services`.

    Returns:
        Whether it is a `409` with the code `services-limit`.
    """
    body = response.get_json()
    return response.status_code == 409 and body["code"] == "services-limit"


def test_default_limit(app: App) -> None:
    """Users may register 50 services by default."""
    with app.app.app_context():
        limit = get_custom_config(CustomConfig).access_control.max_services_per_user
    assert limit == 50


def test_limit_reached(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Users can't register more services than allowed; updates still work.

    Deleting a service frees a place; other users have their own limit.
    """
    _set_limit(app, 2)
    first, second = register(client), register(client)
    response = _post(client)
    assert _limited(response)
    assert "2 services" in response.get_json()["detail"]
    assert _counts(mongo_client) == {ALICE: 2}
    service = {**_service(), "name": "New name"}
    response = client.put(
        f"{BASE}/services/{first}", json=service, headers=headers(ALICE)
    )
    assert response.status_code == 200, response.get_json()
    assert _creator(mongo_client, first) == ALICE
    register(client, BOB)
    response = client.delete(f"{BASE}/services/{second}", headers=headers(ALICE))
    assert response.status_code == 200
    assert _counts(mongo_client) == {ALICE: 1, BOB: 1}
    register(client)
    assert _limited(_post(client))


def test_limit_lowered(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Users above a lowered limit keep their services, but can't add more."""
    first, _ = register(client), register(client)
    _set_limit(app, 1)
    assert _limited(_post(client))
    client.delete(f"{BASE}/services/{first}", headers=headers(ALICE))
    assert _limited(_post(client))
    assert _counts(mongo_client) == {ALICE: 1}


def test_admins_only(app: App, client: FlaskClient) -> None:
    """With a limit of `0`, only admins (in admin mode) register services."""
    _set_limit(app, 0)
    assert _limited(_post(client))
    assert _limited(_post(client, ADMIN, admin_mode=False))
    assert _post(client, ADMIN).status_code == 201


def test_admins_exempt(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Services registered by admins in admin mode count for nobody.

    Also with `PUT` (new services with chosen identifiers).
    """
    _set_limit(app, 1)
    posted = _post(client, ADMIN).get_json()
    response = client.put(
        f"{BASE}/services/ADMSRV", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 201
    assert _creator(mongo_client, posted) is None
    assert _creator(mongo_client, "ADMSRV") is None
    outside = _post(client, ADMIN, admin_mode=False).get_json()
    assert _creator(mongo_client, outside) == ADMIN
    assert _limited(_post(client, ADMIN, admin_mode=False))
    assert _counts(mongo_client) == {ADMIN: 1}


def test_no_limit(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Without a limit (`null`), services are still counted, for a later limit."""
    _set_limit(app, None)
    register(client)
    register(client)
    assert _counts(mongo_client) == {ALICE: 2}
    _set_limit(app, 2)
    assert _limited(_post(client))


def test_not_served(app: App, client: FlaskClient) -> None:
    """The user a service counts for is not served."""
    service_id = register(client)
    service = client.get(f"{BASE}/services/{service_id}").get_json()
    assert CREATED_BY not in service
    services = client.get(f"{BASE}/services").get_json()
    assert all(CREATED_BY not in item for item in services)


def test_kept_whatever_roles(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """A service counts for its creator, whatever its roles, also renamed.

    Giving the service to a group and leaving it doesn't free a place, and
    neither do updates by admins, nor renames.
    """
    _set_limit(app, 1)
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert grant(client, service_id, BOB, "owner") == 200
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/{ALICE}", headers=headers(ALICE)
    )
    assert response.status_code == 200, response.get_json()
    assert _limited(_post(client))
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 200, response.get_json()
    response = client.post(
        f"{BASE}/services/{service_id}/rename",
        json={"id": "NEWSRV"},
        headers=headers(ADMIN),
    )
    assert response.status_code == 200, response.get_json()
    assert _creator(mongo_client, "NEWSRV") == ALICE
    assert _limited(_post(client))
    response = client.delete(f"{BASE}/services/NEWSRV", headers=headers(BOB))
    assert response.status_code == 200
    assert _counts(mongo_client) == {ALICE: 0}
    register(client)


def test_invitations_not_limited(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Users at the limit can still accept invitations as owner.

    The service keeps counting for the user who registered it.
    """
    _set_limit(app, 1)
    register(client, BOB)
    service_id = register(client)
    token = token_of(client, service_id, role="owner")
    assert accept(client, token, user=BOB).status_code == 200
    assert _creator(mongo_client, service_id) == ALICE
    assert _counts(mongo_client) == {ALICE: 1, BOB: 1}


def test_failed_registrations_released(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Registrations that fail don't count: invalid, duplicate or unknown type."""
    _set_limit(app, 1)
    service = _service()
    assert (
        client.post(f"{BASE}/services", json=service, headers=headers(BOB)).status_code
        == 201
    )
    response = client.post(f"{BASE}/services", json=service, headers=headers(ALICE))
    assert response.get_json()["code"] == "duplicate-service"
    invalid = {**_service(), "url": "not a URL"}
    response = client.post(f"{BASE}/services", json=invalid, headers=headers(ALICE))
    assert response.status_code == 400
    assert _counts(mongo_client) == {BOB: 1}
    register(client)


def test_concurrent_duplicate_released(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate registered after the reservation releases it."""
    service = _service()
    store = server.store_pending_type

    def register_first(value: object) -> None:
        """Let another user register the service first.

        Args:
            value: Type of the service.
        """
        monkeypatch.setattr(server, "store_pending_type", store)
        response = client.post(f"{BASE}/services", json=service, headers=headers(BOB))
        assert response.status_code == 201
        store(value)

    monkeypatch.setattr(server, "store_pending_type", register_first)
    response = client.post(f"{BASE}/services", json=service, headers=headers(ALICE))
    assert response.get_json()["code"] == "duplicate-service"
    assert _counts(mongo_client) == {ALICE: 0, BOB: 1}


def test_unregistered_released(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service removed as its owner can't be stored doesn't count."""

    def fail(serviceId: str) -> None:
        """Fail to store the owner.

        Args:
            serviceId: Service identifier.

        Raises:
            ValueError: Always.
        """
        raise ValueError("can't store")

    monkeypatch.setattr(server, "add_owner", fail)
    assert _post(client).status_code == 400
    assert _counts(mongo_client) == {ALICE: 0}
    assert mongo_client[DB]["services"].count_documents({}) == 0


def test_unregistered_deleted_meanwhile(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new service deleted before it is removed is released once."""
    add_owner = server.add_owner

    def delete_then_fail(serviceId: str) -> None:
        """Delete the service as an admin, then fail to store the owner.

        Args:
            serviceId: Service identifier.

        Raises:
            ValueError: Always.
        """
        add_owner(serviceId)
        response = client.delete(f"{BASE}/services/{serviceId}", headers=headers(ADMIN))
        assert response.status_code == 200
        raise ValueError("can't store")

    register(client)
    monkeypatch.setattr(server, "add_owner", delete_then_fail)
    assert _post(client).status_code == 400
    assert _counts(mongo_client) == {ALICE: 1}


def test_reserved_before_stored(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registration that starts while another one is stored sees its place.

    As the place is reserved before the service is stored, a count of the
    stored services would let both pass.
    """
    _set_limit(app, 1)
    store = server.store_pending_type
    responses: list[TestResponse] = []

    def register_meanwhile(value: object) -> None:
        """Let the user register another service meanwhile.

        Args:
            value: Type of the service.
        """
        monkeypatch.setattr(server, "store_pending_type", store)
        responses.append(_post(client))
        store(value)

    monkeypatch.setattr(server, "store_pending_type", register_meanwhile)
    assert _post(client).status_code == 201
    assert _limited(responses[0])
    assert mongo_client[DB]["services"].count_documents({}) == 1


def test_concurrent_registrations(
    app: App, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """Concurrent registrations of a user never exceed the limit."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("atomic writes of MongoDB")
    _set_limit(app, 5)
    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda _: _post(app.app.test_client()), range(30)))
    assert sum(response.status_code == 201 for response in responses) == 5
    assert sum(_limited(response) for response in responses) == 25
    assert mongo_client[DB]["services"].count_documents({CREATED_BY: ALICE}) == 5
    assert _counts(mongo_client) == {ALICE: 5}


def test_concurrent_reservations(app: App) -> None:
    """Concurrent reservations of a user never exceed the limit."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("atomic writes of MongoDB")
    _set_limit(app, 7)

    def reserve(_: int) -> bool:
        """Reserve a place for the user.

        Returns:
            Whether a place was reserved.
        """
        with app.app.app_context():
            try:
                return reserve_service(ALICE)
            except service_limits.ServicesLimit:
                return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert sum(pool.map(reserve, range(64))) == 7


def test_release(app: App, mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Releases never make counters negative; `None` counts for nobody."""
    with app.app.app_context():
        assert reserve_service(None) is False
        release_service(None)
        release_service(ALICE)
        assert reserve_service(ALICE) is True
        release_service(ALICE)
        release_service(ALICE)
    assert _counts(mongo_client) == {ALICE: 0}


def test_limit_logged(
    app: App, client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Refused registrations are logged, with the user."""
    _set_limit(app, 0)
    with caplog.at_level(logging.INFO):
        _post(client)
    assert f"'{ALICE}' registered 0 services" in caplog.text


@pytest.fixture
def stored(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> Iterator[dict[str, str]]:
    """Services stored before the users who registered them were stored.

    Registered by alice and bob, owned by alice alone (`alice`), by bob and
    alice (`two`), by alice's team only (`group`), and with an admin's service
    (`admin`); then the internal field and the counters are removed.

    Yields:
        Service identifiers, by name.
    """
    ids = {
        "alice": register(client),
        "two": register(client, BOB),
        "group": register(client),
        "admin": _post(client, ADMIN).get_json(),
    }
    assert grant(client, ids["two"], ALICE, "owner", as_user=BOB) == 200
    assert grant_group(client, ids["group"], TEAM_GROUP, "owner") == 200
    with app.app.app_context():
        assert remove_resource_role(
            subject=ALICE, role="owner", resource=f"{BASE}/services/{ids['group']}"
        )
    mongo_client[DB]["services"].update_many({}, {"$unset": {CREATED_BY: ""}})
    mongo_client[DB][SERVICE_COUNTS].delete_many({})
    yield ids


def test_creators_added_on_startup(
    stored: dict[str, str], mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """On startup, services count for their only user owner, if any.

    Services already counted keep their user; counters are set.
    """
    mongo_client[DB]["services"].update_one(
        {"id": stored["admin"]}, {"$set": {CREATED_BY: BOB}}
    )
    create_app()
    creators = {name: _creator(mongo_client, id) for name, id in stored.items()}
    assert creators == {"alice": ALICE, "two": None, "group": None, "admin": BOB}
    assert _counts(mongo_client) == {ALICE: 1, BOB: 1}
    create_app()
    assert _counts(mongo_client) == {ALICE: 1, BOB: 1}


def test_creators_added_concurrently(
    app: App,
    stored: dict[str, str],
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Users added by a concurrent run (e.g., another worker) are kept."""
    owner = service_limits._single_user_owner

    def set_first(path: str) -> str | None:
        """Let another run set the user first.

        Args:
            path: Path of the service.

        Returns:
            The only user owner.
        """
        mongo_client[DB]["services"].update_many(
            {CREATED_BY: {"$exists": False}}, {"$set": {CREATED_BY: "other"}}
        )
        return owner(path)

    monkeypatch.setattr(service_limits, "_single_user_owner", set_first)
    with app.app.app_context():
        add_service_creators()
    assert {_creator(mongo_client, id) for id in stored.values()} == {"other"}


def test_counters_reconciled(
    app: App, client: FlaskClient, mongo_client: MongoClient[dict[str, Any]]
) -> None:
    """On startup, counters that drifted are corrected."""
    register(client)
    register(client, BOB)
    counts = mongo_client[DB][SERVICE_COUNTS]
    counts.update_one({"_id": ALICE}, {"$set": {COUNT: 9}})
    counts.delete_one({"_id": BOB})
    counts.insert_one({"_id": "gone", COUNT: 3})
    counts.insert_one({"_id": "zero", COUNT: 0})
    with app.app.app_context():
        reconcile_service_counts()
    assert _counts(mongo_client) == {ALICE: 1, BOB: 1, "gone": 0, "zero": 0}


def test_counters_changed_meanwhile(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counters changed while they are reconciled are kept.

    E.g., by registrations on other instances while the app starts.
    """
    register(client)
    register(client, BOB)
    counts = mongo_client[DB][SERVICE_COUNTS]
    counts.update_one({"_id": ALICE}, {"$set": {COUNT: 9}})
    counts.delete_one({"_id": BOB})
    update_one = COLLECTION_CLASS.update_one

    def change_first(self: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        """Change the counter concurrently, before it is set.

        Args:
            self: Collection.
            query: Filter.
            *args: Further arguments.
            **kwargs: Further keyword arguments.

        Returns:
            Result of the update.
        """
        if self.name == SERVICE_COUNTS and kwargs.get("upsert"):
            update_one(self, {"_id": query["_id"]}, {"$inc": {COUNT: 5}}, upsert=True)
        return update_one(self, query, *args, **kwargs)

    monkeypatch.setattr(COLLECTION_CLASS, "update_one", change_first)
    with app.app.app_context():
        reconcile_service_counts()
    assert _counts(mongo_client) == {ALICE: 14, BOB: 5}


def test_counters_inserted_meanwhile(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A counter inserted while counters are reconciled is kept."""
    register(client)
    mongo_client[DB][SERVICE_COUNTS].delete_many({})
    update_one = COLLECTION_CLASS.update_one

    def duplicate(self: Any, *args: Any, **kwargs: Any) -> Any:
        """Fail as if the counter was inserted concurrently.

        Args:
            self: Collection.
            *args: Arguments of the update.
            **kwargs: Keyword arguments of the update.

        Returns:
            Result of the update, for other collections.

        Raises:
            DuplicateKeyError: For counters.
        """
        if self.name == SERVICE_COUNTS:
            raise DuplicateKeyError("duplicate")
        return update_one(self, *args, **kwargs)

    monkeypatch.setattr(COLLECTION_CLASS, "update_one", duplicate)
    with app.app.app_context():
        reconcile_service_counts()
    assert _counts(mongo_client) == {}


def test_reconciled_logged(
    app: App,
    client: FlaskClient,
    mongo_client: MongoClient[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrections are logged, with the user and the numbers."""
    register(client)
    mongo_client[DB][SERVICE_COUNTS].delete_many({})
    with app.app.app_context(), caplog.at_level(logging.INFO):
        reconcile_service_counts()
    assert f"'{ALICE}' corrected from none to 1" in caplog.text


def test_services_path_on_startup(app: App) -> None:
    """The path of the services is that of the route of `POST /services`."""
    with app.app.app_context():
        assert services_path_on_startup() == f"{BASE}/services"


def test_services_path_missing(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a route of `POST /services`, the path is unknown."""
    monkeypatch.setattr(service_limits, "POST_SERVICES_ENDPOINT", ".nothing")
    with app.app.app_context(), pytest.raises(RuntimeError, match="POST /services"):
        services_path_on_startup()


def test_index(app: App, mongo_client: MongoClient[dict[str, Any]]) -> None:
    """Services are indexed by the user they count for."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("indexes of MongoDB")
    indexes = mongo_client[DB]["services"].index_information().values()
    assert [(CREATED_BY, 1)] in [index["key"] for index in indexes]
