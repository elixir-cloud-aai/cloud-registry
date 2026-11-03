"""Tests for listing the registry among its own services.

With the app created from its config; the app and its fixtures are set up in
`tests/conftest.py`.
"""

from datetime import UTC, datetime
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca import get_config, get_custom_config
from foca.security.access_control import add_resource_role, list_roles
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry import self_listing
from cloud_registry.ga4gh.registry.self_listing import (
    _refresh,
    refresh_self_listing,
)
from cloud_registry.ga4gh.registry.service import (
    CREATED_BY,
    KEY,
    SELF_ENTRY,
    VERIFIED,
    DuplicateService,
    RegisterService,
)
from cloud_registry.ga4gh.registry.service_limits import SERVICE_COUNTS
from cloud_registry.service_models.custom_config import CustomConfig
from tests.conftest import ADMIN, COLLECTION_CLASS, USER, make_headers

BASE = "/ga4gh/registry/v1"
LISTING = f"{BASE}/service-info/listing"
ENTRY_ID = "org.example.cloud-registry"
ENTRY_URL = f"https://registry.org{BASE}"
ENTRY_TYPE = {"group": "org.ga4gh", "artifact": "service-registry", "version": "1.0.0"}
OTHER_SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}


@pytest.fixture
def admin() -> dict[str, str]:
    """Request headers of an admin, in admin mode."""
    return make_headers(ADMIN)


@pytest.fixture
def db(mongo_client: MongoClient[dict[str, Any]]) -> Any:
    """Database of the app."""
    return mongo_client["serviceStore"]


def _list(client: FlaskClient, admin: dict[str, str]) -> TestResponse:
    """List the registry, which must succeed.

    Args:
        client: Test client.
        admin: Request headers of an admin.

    Returns:
        Response.
    """
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 201, response.get_json()
    return response


def _service_info(app: App) -> dict[str, Any]:
    """Get the configured service info of an app, to change it.

    Args:
        app: Connexion app.

    Returns:
        The service info (`service_info.service`).
    """
    with app.app.app_context():
        return get_config().service_info.service  # type: ignore[union-attr]


def _custom(app: App) -> CustomConfig:
    """Get the custom config of an app.

    Args:
        app: Connexion app.

    Returns:
        Custom config.
    """
    with app.app.app_context():
        return get_custom_config(CustomConfig)


def _refresh_entry(app: App) -> None:
    """Refresh the entry, as on startup.

    Args:
        app: Connexion app.
    """
    with app.app.app_context():
        refresh_self_listing()


def test_not_listed(client: FlaskClient, admin: dict[str, str]) -> None:
    """The registry isn't listed by default."""
    response = client.get(LISTING, headers=admin)
    assert response.status_code == 200
    assert response.get_json() == {"listed": False}
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_admins_only(client: FlaskClient, method: str) -> None:
    """Only admins, in admin mode, may use the operations."""
    call = getattr(client, method)
    assert call(LISTING).status_code == 401
    assert call(LISTING, headers=make_headers(USER)).status_code == 403
    assert call(LISTING, headers=make_headers(ADMIN, roles=None)).status_code == 403


def test_list(client: FlaskClient, admin: dict[str, str]) -> None:
    """Listing creates the entry from the service info, with its type."""
    response = _list(client, admin)
    assert response.get_json() == {"listed": True, "service_id": ENTRY_ID}
    assert response.headers["Location"] == f"{ENTRY_URL}/services/{ENTRY_ID}"
    assert client.get(LISTING, headers=admin).get_json() == response.get_json()
    info = client.get(f"{BASE}/service-info").get_json()
    service = client.get(f"{BASE}/services/{ENTRY_ID}").get_json()
    assert service["url"] == ENTRY_URL
    assert service["createdAt"] == service["updatedAt"]
    for field in ("id", "name", "type", "description", "organization", "version"):
        assert service[field] == info[field]
    assert service["environment"] == info["environment"]
    assert service["documentationUrl"] == info["documentationUrl"]
    # neither the rules of the service info nor internal fields are served
    assert not {"registration_rules", KEY, VERIFIED, CREATED_BY, SELF_ENTRY} & set(
        service
    )
    assert client.get(f"{BASE}/services").get_json() == [service]
    assert ENTRY_TYPE in client.get(f"{BASE}/services/types").get_json()


def test_list_verified_without_owner(
    client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """The entry counts as verified, has no owner and counts for nobody."""
    _list(client, admin)
    verification = client.get(f"{BASE}/services/{ENTRY_ID}/verification")
    assert verification.status_code == 200
    assert verification.get_json()["url"] == ENTRY_URL
    roles = client.get(f"{BASE}/services/{ENTRY_ID}/permissions", headers=admin)
    assert roles.get_json() == []
    assert client.get(f"{BASE}/me", headers=admin).get_json()["services"] == []
    assert db["services"].find_one({"id": ENTRY_ID})[CREATED_BY] is None
    assert db[SERVICE_COUNTS].count_documents({}) == 0


def test_list_removes_left_roles(
    app: App, client: FlaskClient, admin: dict[str, str]
) -> None:
    """Roles left on the identifier by a deleted service don't apply."""
    path = f"{BASE}/services/{ENTRY_ID}"
    with app.app.app_context():
        add_resource_role(USER, "owner", path)
    _list(client, admin)
    with app.app.app_context():
        assert list_roles(path) == []


def test_list_again_keeps_entry(
    client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Listing again changes nothing if the entry is up to date."""
    _list(client, admin)
    stored = db["services"].find_one({"id": ENTRY_ID})
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 200
    assert response.get_json() == {"listed": True, "service_id": ENTRY_ID}
    assert "Location" not in response.headers
    assert db["services"].find_one({"id": ENTRY_ID}) == stored


def test_list_again_refreshes_entry(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Listing again brings the entry up to date, also its type."""
    _list(client, admin)
    new_type = {**ENTRY_TYPE, "version": "1.1.0"}
    _service_info(app).update(type=new_type, description="New")
    assert client.put(LISTING, headers=admin).status_code == 200
    service = db["services"].find_one({"id": ENTRY_ID})
    assert service["description"] == "New"
    assert new_type in client.get(f"{BASE}/services/types").get_json()


def test_list_environment_not_allowed(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Environments not allowed are refused; nothing is stored."""
    _custom(app).endpoints.services.environments = ["production"]
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 400
    assert (
        "'environment' must be one of: 'production'." in response.get_json()["detail"]
    )
    assert db["services"].count_documents({}) == 0
    assert db["service_types"].count_documents({}) == 0


def test_list_url_scheme_not_allowed(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Public URLs with a scheme not allowed for services are refused."""
    with app.app.app_context():
        get_config().server.public_url = "http://registry.org/"
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 400
    assert db["services"].count_documents({}) == 0


@pytest.mark.parametrize("service_id", ["", ".", "types", "a/b", "a,b"])
def test_list_invalid_id(
    app: App, client: FlaskClient, admin: dict[str, str], service_id: str
) -> None:
    """Identifiers of the service info that services can't have are refused."""
    _service_info(app)["id"] = service_id
    assert client.put(LISTING, headers=admin).status_code == 400
    assert client.get(LISTING, headers=admin).get_json() == {"listed": False}


def test_list_duplicate(client: FlaskClient, admin: dict[str, str], db: Any) -> None:
    """A service with the same URL and API type is named, not taken over."""
    service = {**OTHER_SERVICE, "url": ENTRY_URL, "type": ENTRY_TYPE}
    assert (
        client.put(f"{BASE}/services/MANUAL", json=service, headers=admin).status_code
        == 201
    )
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == "duplicate-service"
    assert response.get_json()["serviceId"] == "MANUAL"
    assert SELF_ENTRY not in db["services"].find_one({"id": "MANUAL"})


def test_list_id_taken(client: FlaskClient, admin: dict[str, str], db: Any) -> None:
    """Another service with the identifier stays; a new type is removed."""
    url = f"{BASE}/services/{ENTRY_ID}"
    assert client.put(url, json=OTHER_SERVICE, headers=admin).status_code == 201
    stored = db["services"].find_one({"id": ENTRY_ID})
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-id-taken"
    assert db["services"].find_one({"id": ENTRY_ID}) == stored
    assert ENTRY_TYPE not in client.get(f"{BASE}/services/types").get_json()


def test_list_id_taken_keeps_stored_type(
    client: FlaskClient, admin: dict[str, str]
) -> None:
    """A type that was stored before the failed operation stays."""
    types = f"{BASE}/services/types"
    assert client.post(types, json=ENTRY_TYPE, headers=admin).status_code == 201
    url = f"{BASE}/services/{ENTRY_ID}"
    assert client.put(url, json=OTHER_SERVICE, headers=admin).status_code == 201
    assert client.put(LISTING, headers=admin).status_code == 409
    assert ENTRY_TYPE in client.get(types).get_json()


def test_list_type_pending(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Pending types are not approved by listing: the operation is refused."""
    _custom(app).access_control.service_types = "moderated"
    types = f"{BASE}/services/types"
    response = client.post(types, json=ENTRY_TYPE, headers=make_headers(USER))
    assert response.status_code == 201
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == "pending-service"
    assert db["services"].count_documents({}) == 0
    assert db["service_types"].find_one({"artifact": "service-registry"})["pending"]


def test_list_type_pending_replaced_stays(
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending type stored right after the operation added it stays."""
    db["service_types"].insert_one({**ENTRY_TYPE, "pending": True})
    monkeypatch.setattr(self_listing, "store_service_type", lambda *args: True)
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 409
    assert db["service_types"].count_documents(ENTRY_TYPE) == 1


def test_list_moderated(app: App, client: FlaskClient, admin: dict[str, str]) -> None:
    """In mode `moderated`, the type and the entry aren't pending."""
    _custom(app).access_control.service_types = "moderated"
    _list(client, admin)
    assert client.get(f"{BASE}/services/{ENTRY_ID}").status_code == 200
    assert client.get(f"{BASE}/services/{ENTRY_ID}/verification").status_code == 200


def test_list_concurrently(
    client: FlaskClient,
    admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry stored by a concurrent request is not an error."""
    _list(client, admin)
    stored = self_listing._stored_entry
    reads: list[None] = []

    def read_missing_first() -> dict[str, Any] | None:
        reads.append(None)
        return None if len(reads) == 1 else stored()

    monkeypatch.setattr(self_listing, "_stored_entry", read_missing_first)
    response = client.put(LISTING, headers=admin)
    assert response.status_code == 200
    assert response.get_json() == {"listed": True, "service_id": ENTRY_ID}


def test_list_removed_concurrently(
    client: FlaskClient,
    admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry removed while it is brought up to date isn't found."""
    _list(client, admin)
    monkeypatch.setattr(self_listing, "_refresh", lambda *args: False)
    assert client.put(LISTING, headers=admin).status_code == 404


def test_list_roles_not_revoked(
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If roles can't be revoked, neither the entry nor a new type stays."""

    def fail(service_id: str) -> None:
        raise RuntimeError(service_id)

    monkeypatch.setattr(self_listing, "remove_permissions", fail)
    assert client.put(LISTING, headers=admin).status_code == 500
    assert db["services"].count_documents({}) == 0
    assert db["service_types"].count_documents({}) == 0


def test_list_again_stores_missing_type(
    client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Listing again repairs an entry whose type was removed meanwhile."""
    _list(client, admin)
    db["service_types"].delete_many({})
    assert client.put(LISTING, headers=admin).status_code == 200
    assert ENTRY_TYPE in client.get(f"{BASE}/services/types").get_json()


def test_unlist(app: App, client: FlaskClient, admin: dict[str, str], db: Any) -> None:
    """Removing the entry removes its type and any roles on it."""
    _list(client, admin)
    path = f"{BASE}/services/{ENTRY_ID}"
    with app.app.app_context():
        add_resource_role(USER, "owner", path)
    response = client.delete(LISTING, headers=admin)
    assert response.status_code == 200
    assert response.get_json() == {"listed": False}
    assert client.get(LISTING, headers=admin).get_json() == {"listed": False}
    assert client.get(path).status_code == 404
    assert ENTRY_TYPE not in client.get(f"{BASE}/services/types").get_json()
    with app.app.app_context():
        assert list_roles(path) == []


def test_unlist_keeps_type_of_services(
    client: FlaskClient, admin: dict[str, str]
) -> None:
    """The type stays if other services have it."""
    _list(client, admin)
    service = {**OTHER_SERVICE, "type": ENTRY_TYPE}
    response = client.post(f"{BASE}/services", json=service, headers=admin)
    assert response.status_code == 201
    assert client.delete(LISTING, headers=admin).status_code == 200
    assert ENTRY_TYPE in client.get(f"{BASE}/services/types").get_json()


def test_unlist_invalid_stored_type(
    client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """An entry whose stored type isn't valid is removed without type."""
    _list(client, admin)
    db["services"].update_one({"id": ENTRY_ID}, {"$set": {"type": "invalid"}})
    assert client.delete(LISTING, headers=admin).status_code == 200
    assert client.get(f"{BASE}/services/{ENTRY_ID}").status_code == 404


def test_unlist_not_listed(client: FlaskClient, admin: dict[str, str]) -> None:
    """Removing an entry that doesn't exist isn't found."""
    assert client.delete(LISTING, headers=admin).status_code == 404


def test_unlist_concurrently(
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry removed concurrently isn't found."""
    _list(client, admin)
    stored = db["services"].find_one({"id": ENTRY_ID})
    db["services"].delete_one({"id": ENTRY_ID})
    monkeypatch.setattr(self_listing, "_stored_entry", lambda: stored)
    assert client.delete(LISTING, headers=admin).status_code == 404


def test_unlist_removed_while_locking(
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry removed before it is locked isn't found."""
    _list(client, admin)
    stored = db["services"].find_one({"id": ENTRY_ID})
    reads = iter([stored, None])
    monkeypatch.setattr(self_listing, "_stored_entry", lambda: next(reads))
    assert client.delete(LISTING, headers=admin).status_code == 404


@pytest.mark.parametrize("method", ["put", "delete"])
def test_renamed_while_locking(
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    """An entry renamed before it is locked is left alone: the request is busy."""
    _list(client, admin)
    stale = db["services"].find_one({"id": ENTRY_ID})
    url = f"{BASE}/services/{ENTRY_ID}/rename"
    assert client.post(url, json={"id": "renamed"}, headers=admin).status_code == 200
    read = self_listing._stored_entry
    reads = iter([stale])
    monkeypatch.setattr(self_listing, "_stored_entry", lambda: next(reads, read()))
    response = getattr(client, method)(LISTING, headers=admin)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-busy"
    assert db["services"].find_one({"id": "renamed"})[SELF_ENTRY] is True


def test_unlist_roles_not_revoked(
    client: FlaskClient,
    admin: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If roles can't be revoked, the entry stays, so that it can be repeated."""
    _list(client, admin)

    def fail(service_id: str) -> None:
        raise RuntimeError(service_id)

    with monkeypatch.context() as patch:
        patch.setattr(self_listing, "remove_permissions", fail)
        assert client.delete(LISTING, headers=admin).status_code == 500
    assert client.get(LISTING, headers=admin).get_json()["listed"] is True
    assert client.delete(LISTING, headers=admin).status_code == 200


def test_rename_keeps_listing(
    app: App, client: FlaskClient, admin: dict[str, str]
) -> None:
    """A renamed entry stays listed, also after it is refreshed."""
    _list(client, admin)
    url = f"{BASE}/services/{ENTRY_ID}/rename"
    assert client.post(url, json={"id": "registry"}, headers=admin).status_code == 200
    assert client.get(LISTING, headers=admin).get_json() == {
        "listed": True,
        "service_id": "registry",
    }
    _service_info(app)["description"] = "New"
    _refresh_entry(app)
    service = client.get(f"{BASE}/services/registry").get_json()
    assert service["description"] == "New"
    assert service["id"] == "registry"
    assert client.delete(LISTING, headers=admin).status_code == 200


def test_update_keeps_listing(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """An admin's update keeps the entry listed, until it is refreshed."""
    _list(client, admin)
    service = {**OTHER_SERVICE, "url": ENTRY_URL, "type": ENTRY_TYPE}
    url = f"{BASE}/services/{ENTRY_ID}"
    assert client.put(url, json=service, headers=admin).status_code == 200
    assert client.get(LISTING, headers=admin).get_json()["listed"] is True
    assert db["services"].find_one({"id": ENTRY_ID})[SELF_ENTRY] is True
    _refresh_entry(app)
    assert client.get(url).get_json()["name"] == "Cloud Registry"


def test_other_updates_not_marked(
    client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Updates of other services don't mark them."""
    url = f"{BASE}/services/OTHER"
    assert client.put(url, json=OTHER_SERVICE, headers=admin).status_code == 201
    assert client.put(url, json=OTHER_SERVICE, headers=admin).status_code == 200
    assert SELF_ENTRY not in db["services"].find_one({"id": "OTHER"})


def test_delete_service_ends_listing(
    client: FlaskClient, admin: dict[str, str]
) -> None:
    """An admin who deletes the entry as a service ends the listing."""
    _list(client, admin)
    url = f"{BASE}/services/{ENTRY_ID}"
    assert client.delete(url, headers=admin).status_code == 200
    assert client.get(LISTING, headers=admin).get_json() == {"listed": False}


def test_refresh_not_listed(app: App, db: Any) -> None:
    """Nothing is stored unless the registry is listed."""
    _refresh_entry(app)
    assert db["services"].count_documents({}) == 0


def test_refresh_unchanged(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """An entry that is up to date isn't written."""
    _list(client, admin)
    stored = db["services"].find_one({"id": ENTRY_ID})
    _refresh_entry(app)
    assert db["services"].find_one({"id": ENTRY_ID}) == stored


def test_refresh_follows_service_info(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Fields follow the service info; the verification stays, with its time."""
    _list(client, admin)
    before = db["services"].find_one({"id": ENTRY_ID})
    db["services"].update_one(
        {"id": ENTRY_ID}, {"$set": {"updatedAt": "2020-01-01T00:00:00.000Z"}}
    )
    info = _service_info(app)
    info.update(version="9.9.9", description="New", documentationUrl=None)
    _refresh_entry(app)
    after = db["services"].find_one({"id": ENTRY_ID})
    assert after["version"] == "9.9.9"
    assert after["description"] == "New"
    assert "documentationUrl" not in after
    assert after["createdAt"] == before["createdAt"]
    assert after["updatedAt"] != "2020-01-01T00:00:00.000Z"
    assert after[VERIFIED] == before[VERIFIED]
    assert after[SELF_ENTRY] is True


def test_refresh_new_url_verified_again(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """A new public URL changes the entry's key and renews its verification."""
    _list(client, admin)
    earlier = datetime(2020, 1, 1, tzinfo=UTC)
    db["services"].update_one({"id": ENTRY_ID}, {"$set": {VERIFIED: earlier}})
    before = db["services"].find_one({"id": ENTRY_ID})
    with app.app.app_context():
        get_config().server.public_url = "https://new.example.org/"
    _refresh_entry(app)
    after = db["services"].find_one({"id": ENTRY_ID})
    assert after["url"] == f"https://new.example.org{BASE}"
    assert after[KEY] != before[KEY]
    assert after[VERIFIED].replace(tzinfo=UTC) > earlier


def test_refresh_restores_verification(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """A removed verification is set again, without changing `updatedAt`."""
    _list(client, admin)
    url = f"{BASE}/services/{ENTRY_ID}/verification"
    assert client.delete(url, headers=admin).status_code == 200
    before = db["services"].find_one({"id": ENTRY_ID})
    _refresh_entry(app)
    after = db["services"].find_one({"id": ENTRY_ID})
    assert VERIFIED in after
    assert after["updatedAt"] == before["updatedAt"]


@pytest.mark.parametrize("change", ["scheme", "duplicate", "pending"])
def test_refresh_refused_keeps_entry(
    app: App,
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    caplog: pytest.LogCaptureFixture,
    change: str,
) -> None:
    """Entries the service info no longer fits are kept, with a warning."""
    _list(client, admin)
    new_type = {**ENTRY_TYPE, "artifact": "other"}
    _service_info(app).update(type=new_type, description="New")
    if change == "scheme":
        _custom(app).endpoints.services.url_schemes = ["http"]
    elif change == "duplicate":
        service = {**OTHER_SERVICE, "url": ENTRY_URL, "type": new_type}
        response = client.post(f"{BASE}/services", json=service, headers=admin)
        assert response.status_code == 201
    else:
        db["service_types"].insert_one({**new_type, "pending": True})
    stored = db["services"].find_one({"id": ENTRY_ID})
    _refresh_entry(app)
    assert db["services"].find_one({"id": ENTRY_ID}) == stored
    assert "not updated" in caplog.text


def test_refresh_race_with_duplicate(
    app: App,
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A duplicate key that is gone again doesn't stop the app from starting."""
    _list(client, admin)
    _service_info(app)["description"] = "New"

    def conflict(*args: Any, **kwargs: Any) -> None:
        raise DuplicateKeyError("E11000")

    monkeypatch.setattr(COLLECTION_CLASS, "update_one", conflict)
    _refresh_entry(app)
    assert "not updated" in caplog.text


def test_refresh_names_duplicate(
    app: App, client: FlaskClient, admin: dict[str, str], db: Any
) -> None:
    """Duplicates registered concurrently are named."""
    _list(client, admin)
    new_type = {**ENTRY_TYPE, "artifact": "other"}
    _service_info(app)["type"] = new_type
    with app.app.app_context():
        stored = db["services"].find_one({"id": ENTRY_ID})
        service = RegisterService(self_listing._entry_data(), id=ENTRY_ID)
        key = service.validate()
        other = {**OTHER_SERVICE, "url": ENTRY_URL, "type": new_type}
        db["services"].insert_one({**other, "id": "OTHER", KEY: key})
        with pytest.raises(DuplicateService):
            _refresh(service, stored, key)


def test_create_app_refreshes_entry(
    app: App,
    client: FlaskClient,
    admin: dict[str, str],
    db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The app brings the entry up to date on startup, and stores a new type."""
    _list(client, admin)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "server:\n  public_url: https://registry.org/\n"
        "security:\n  auth:\n    issuers: [https://login.example.org/oidc/]\n"
        "    audience: [client-id]\n"
        "  access_control:\n    require_admin: false\n"
        "service_info:\n  service:\n    description: New\n"
        "    type: {group: org.ga4gh, artifact: service-registry, version: 2.0.0}\n"
    )
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(overlay))
    create_app()
    service = db["services"].find_one({"id": ENTRY_ID})
    assert service["description"] == "New"
    assert service["type"]["version"] == "2.0.0"
    assert db["service_types"].count_documents({"version": "2.0.0"}) == 1
    # the former type stays, as for updates of other services
    assert db["service_types"].count_documents({"version": "1.0.0"}) == 1
