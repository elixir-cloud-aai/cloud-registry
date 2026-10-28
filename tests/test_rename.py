"""Tests for renaming services, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`; the helpers for
roles are those of `tests/test_access_control.py`.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
import re
import time
from typing import Any
from urllib.parse import quote

from connexion import App
from flask.testing import FlaskClient
from foca.security.access_control import add_resource_role, list_roles
import pymongo
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry import locks as locks_module
from cloud_registry.ga4gh.registry import rename as rename_module
from cloud_registry.ga4gh.registry.permissions import USER_NAMES
from cloud_registry.ga4gh.registry.service import VERIFIED
from cloud_registry.utils import get_collection, now
from tests.conftest import ADMIN, NAMES, TEAM_GROUP
from tests.test_access_control import (
    ALICE,
    BASE,
    BOB,
    before_first_call,
    grant,
    grant_group,
    group_path,
    headers,
    permission_list,
    register,
)

NEW_ID = "NEWSRV"
# Path of the services, under which roles are granted
PATH = f"{BASE}/services"
# Age of locks that are stale
STALE = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't wait for locks, and don't resume renames before requests.

    Args:
        monkeypatch: Fixture to patch the waits and the last resume.
    """
    monkeypatch.setattr(locks_module, "LOCK_WAIT", 0)
    monkeypatch.setattr(locks_module, "LOCK_ATTEMPTS", 2)
    monkeypatch.setattr(rename_module, "_last_resume", time.monotonic())
    monkeypatch.setattr(rename_module, "RESUME_INTERVAL", float("inf"))


def rename(
    client: FlaskClient,
    service_id: str,
    new_id: Any = NEW_ID,
    user: str | None = ADMIN,
) -> TestResponse:
    """Rename a service.

    Args:
        client: Test client of the app.
        service_id: Identifier of the service.
        new_id: New identifier (`id` of the request body).
        user: User who renames the service; no token if `None`.

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/services/{service_id}/rename",
        json={"id": new_id},
        headers=headers(user),
    )


def get(client: FlaskClient, service_id: str) -> TestResponse:
    """Get a service, as an admin.

    Args:
        client: Test client of the app.
        service_id: Identifier of the service.

    Returns:
        Response.
    """
    return client.get(f"{BASE}/services/{service_id}", headers=headers(ADMIN))


def roles(client: FlaskClient, service_id: str) -> list[dict[str, Any]]:
    """List the roles on a service, sorted.

    Args:
        client: Test client of the app.
        service_id: Identifier of the service.

    Returns:
        Roles of users and groups.
    """
    return sorted(permission_list(client, service_id), key=repr)


def stored_service(app: App, service_id: str) -> dict[str, Any] | None:
    """Read a stored service, with its internal fields.

    Args:
        app: App.
        service_id: Identifier of the service.

    Returns:
        Stored service, or `None`.
    """
    with app.app.app_context():
        return get_collection("services").find_one({"id": service_id}, {"_id": False})


def locks(app: App) -> list[dict[str, Any]]:
    """Read the stored locks.

    Args:
        app: App.

    Returns:
        Locks, without their times, sorted.
    """
    with app.app.app_context():
        found = get_collection(locks_module.LOCKS).find({}, {"started_at": False})
        return sorted(found, key=repr)


def lock_ids(app: App) -> list[Any]:
    """Get the identifiers of the stored locks.

    Args:
        app: App.

    Returns:
        Identifiers (`_id`) of the locks, sorted.
    """
    return sorted((lock["_id"] for lock in locks(app)), key=repr)


def resume(app: App) -> None:
    """Resume renames whose locks are stale, as on startup.

    Args:
        app: App.
    """
    with app.app.app_context():
        rename_module.resume_renames()


def make_lock(
    app: App, source: str, target: str = NEW_ID, age: timedelta = STALE
) -> None:
    """Store the locks of a rename, as if it was interrupted.

    Args:
        app: App.
        source: Identifier of the renamed service.
        target: New identifier.
        age: How long ago the rename started.
    """
    started_at = now() - age
    with app.app.app_context():
        get_collection(locks_module.LOCKS).insert_many(
            [
                {
                    "_id": source,
                    "to": target,
                    "path": PATH,
                    "token": "a",
                    "started_at": started_at,
                },
                {
                    "_id": target,
                    "from": source,
                    "token": "b",
                    "started_at": started_at,
                },
            ]
        )


def make_change_lock(app: App, service_id: str, age: timedelta) -> None:
    """Store the lock of a change of a service, as if it was running.

    Args:
        app: App.
        service_id: Identifier of the service.
        age: How long ago the change started.
    """
    with app.app.app_context():
        get_collection(locks_module.LOCKS).insert_one(
            {
                "_id": {"change": "c"},
                "service": service_id,
                "started_at": now() - age,
            }
        )


def move_service(app: App, source: str, target: str) -> None:
    """Change the identifier of a stored service, as a rename does first.

    Args:
        app: App.
        source: Identifier of the service.
        target: New identifier.
    """
    with app.app.app_context():
        get_collection("services").update_one({"id": source}, {"$set": {"id": target}})


@pytest.fixture
def service_id(client: FlaskClient) -> str:
    """Register a service with roles of users and a group, and verify it.

    Args:
        client: Test client of the app.

    Returns:
        Identifier of the service, owned by alice (with her name stored), with
        bob as maintainer and the team group as owner.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    response = client.put(
        f"{BASE}/services/{service_id}/verification", headers=headers(ADMIN)
    )
    assert response.status_code == 200
    return service_id


EXPECTED_ROLES = sorted(
    [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"},
        {"user_id": BOB, "role": "maintainer"},
        {"group": TEAM_GROUP, "role": "owner"},
    ],
    key=repr,
)


def test_rename(app: App, client: FlaskClient, service_id: str) -> None:
    """Renamed services keep their roles, names, verification and `createdAt`.

    They are only found under the new identifier; `updatedAt` is renewed.
    """
    with app.app.app_context():
        get_collection("services").update_one(
            {"id": service_id}, {"$set": {"updatedAt": "2020-01-01T00:00:00Z"}}
        )
    before = stored_service(app, service_id)
    assert before is not None
    response = rename(client, service_id)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == NEW_ID
    after = stored_service(app, NEW_ID)
    assert after is not None
    assert after["createdAt"] == before["createdAt"]
    assert after["updatedAt"] > before["updatedAt"]
    assert after[VERIFIED] == before[VERIFIED]
    assert get(client, NEW_ID).get_json()["id"] == NEW_ID
    assert get(client, service_id).status_code == 404
    verification = client.get(f"{BASE}/services/{NEW_ID}/verification")
    assert verification.status_code == 200
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(ADMIN)
    )
    assert response.status_code == 404
    assert locks(app) == []
    with app.app.app_context():
        assert not get_collection(USER_NAMES).count_documents(
            {"service_id": service_id}
        )


def test_renamed_roles_apply(client: FlaskClient, service_id: str) -> None:
    """Roles apply under the new identifier only."""
    assert rename(client, service_id).status_code == 200
    me = client.get(f"{BASE}/me", headers=headers(BOB)).get_json()
    assert me["services"] == [{"service_id": NEW_ID, "role": "maintainer"}]
    response = client.delete(f"{BASE}/services/{NEW_ID}", headers=headers(ALICE))
    assert response.status_code == 200


def test_rename_logged(
    client: FlaskClient, service_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Renames are logged, with the admin."""
    caplog.set_level("INFO")
    assert rename(client, service_id).status_code == 200
    assert f"Service '{service_id}' renamed to '{NEW_ID}' by '{ADMIN}'." in caplog.text


@pytest.mark.parametrize("user, status", [(None, 401), (ALICE, 403), (BOB, 403)])
def test_rename_admins_only(
    client: FlaskClient, service_id: str, user: str | None, status: int
) -> None:
    """Only admins rename services, not even owners."""
    assert rename(client, service_id, user=user).status_code == status
    assert get(client, service_id).status_code == 200


def test_rename_unknown(client: FlaskClient) -> None:
    """Unknown services are not found."""
    assert rename(client, "UNKNWN").status_code == 404
    assert get(client, NEW_ID).status_code == 404


def _other_service() -> dict[str, Any]:
    """Get another service than those `register()` registers.

    Returns:
        Service with its own URL.
    """
    return {
        "name": "Other",
        "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
        "organization": {"name": "Other", "url": "https://example.org"},
        "version": "1.0.0",
        "url": "https://other.example.org/ga4gh/tes/v1",
    }


@pytest.mark.parametrize("own", [False, True], ids=["other", "own"])
def test_rename_taken(client: FlaskClient, service_id: str, own: bool) -> None:
    """Identifiers of registered services, also its own, are taken (`409`)."""
    new_id = service_id if own else NEW_ID
    if not own:
        response = client.put(
            f"{BASE}/services/{new_id}", json=_other_service(), headers=headers(ADMIN)
        )
        assert response.status_code == 201, response.get_json()
    response = rename(client, service_id, new_id=new_id)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-id-taken"
    assert roles(client, service_id) == EXPECTED_ROLES


@pytest.mark.parametrize(
    "new_id",
    [".", "..", "types", "", "a/b", "a,b", "a\nb", "\ud800", 1, None],
)
def test_rename_invalid(client: FlaskClient, service_id: str, new_id: Any) -> None:
    """Identifiers that can't be routed or stored in roles are rejected."""
    response = rename(client, service_id, new_id=new_id)
    assert response.status_code == 400
    assert roles(client, service_id) == EXPECTED_ROLES


@pytest.mark.parametrize("body", [None, {}, {"id": "A", "x": 1}])
def test_rename_invalid_body(
    client: FlaskClient, service_id: str, body: dict[str, Any] | None
) -> None:
    """Bodies need the new identifier, and nothing else."""
    response = client.post(
        f"{BASE}/services/{service_id}/rename", json=body, headers=headers(ADMIN)
    )
    assert response.status_code == 400
    assert get(client, service_id).status_code == 200


@pytest.mark.parametrize("new_id", ["my tes", "a.b", "...", "Types", "#a", "ü"])
def test_rename_special(client: FlaskClient, service_id: str, new_id: str) -> None:
    """Identifiers that clients can request (encoded) are accepted."""
    assert rename(client, service_id, new_id=new_id).status_code == 200
    encoded = quote(new_id, safe="")
    assert get(client, encoded).get_json()["id"] == new_id
    assert roles(client, encoded) == EXPECTED_ROLES


@pytest.mark.parametrize("locked", ["source", "target"])
def test_rename_locked(
    app: App, client: FlaskClient, service_id: str, locked: str
) -> None:
    """Identifiers of running renames can't be renamed (`409`).

    Neither as old nor as new identifier.
    """
    other = register(client)
    if locked == "source":
        make_lock(app, service_id, "X", age=timedelta(0))
        source = service_id
    else:
        make_lock(app, "X", NEW_ID, age=timedelta(0))
        source = other
    before = locks(app)
    response = rename(client, source)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-busy"
    assert get(client, source).status_code == 200
    assert locks(app) == before


@pytest.mark.parametrize("identifier", ["source", "target"])
def test_rename_waits_for_changes(
    app: App, client: FlaskClient, service_id: str, identifier: str
) -> None:
    """Renames don't start while either service is changed (`409`)."""
    make_change_lock(
        app, service_id if identifier == "source" else NEW_ID, timedelta(0)
    )
    before = locks(app)
    response = rename(client, service_id)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-busy"
    assert locks(app) == before
    assert roles(client, service_id) == EXPECTED_ROLES


def test_rename_ignores_stale_changes(
    app: App, client: FlaskClient, service_id: str
) -> None:
    """Locks of changes that are stale (e.g., crashed) don't block renames."""
    make_change_lock(app, service_id, STALE)
    assert rename(client, service_id).status_code == 200
    assert roles(client, NEW_ID) == EXPECTED_ROLES


def _changes(service_id: str) -> list[tuple[str, str, dict[str, Any] | None, str]]:
    """Get changes of a service, as requests.

    Args:
        service_id: Identifier of the service.

    Returns:
        Method, path, body and user of each change.
    """
    service = f"{BASE}/services/{service_id}"
    role = {"role": "owner"}
    return [
        ("PUT", service, _other_service(), ALICE),
        ("PUT", service, _other_service(), ADMIN),
        ("DELETE", service, None, ALICE),
        ("PUT", f"{service}/permissions/{BOB}", role, ALICE),
        ("DELETE", f"{service}/permissions/{BOB}", None, ALICE),
        ("PUT", group_path(service_id, TEAM_GROUP), role, ALICE),
        ("DELETE", group_path(service_id, TEAM_GROUP), None, ALICE),
    ]


def _send(client: FlaskClient, change: tuple[Any, ...]) -> TestResponse:
    """Send a change of a service.

    Args:
        client: Test client of the app.
        change: Method, path, body and user (cf. `_changes()`).

    Returns:
        Response.
    """
    method, path, body, user = change
    return client.open(path, method=method, json=body, headers=headers(user))


@pytest.mark.parametrize("index", range(len(_changes(""))))
def test_changes_wait_for_renames(
    app: App, client: FlaskClient, service_id: str, index: int
) -> None:
    """Services aren't changed while they are renamed (`409`)."""
    make_lock(app, service_id, age=timedelta(0))
    response = _send(client, _changes(service_id)[index])
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["code"] == "service-busy"
    assert roles(client, service_id) == EXPECTED_ROLES
    assert lock_ids(app) == sorted([service_id, NEW_ID], key=repr)


@pytest.mark.parametrize("index", range(len(_changes(""))))
def test_changes_during_rename(
    app: App,
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
) -> None:
    """Changes sent while a service moves don't interleave with the rename.

    E.g., a revoked role would otherwise move to the new identifier, or a
    deletion leave the renamed service without roles.
    """
    responses: list[TestResponse] = []
    change = _changes(service_id)[index]

    def send() -> None:
        responses.append(_send(client, change))

    with monkeypatch.context() as patch:
        before_first_call(patch, "update_one", "services", send)
        assert rename(client, service_id).status_code == 200
    assert responses[0].status_code == 409
    assert responses[0].get_json()["code"] == "service-busy"
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert get(client, service_id).status_code == 404
    assert locks(app) == []


def test_change_retried_after_rename(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changes that wait for a rename go ahead once it finished."""

    def finish() -> None:
        with app.app.app_context():
            get_collection(locks_module.LOCKS).delete_many({})

    make_lock(app, service_id, age=timedelta(0))
    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "delete_one", locks_module.LOCKS, finish)
        response = _send(client, _changes(service_id)[2])
    assert fired
    assert response.status_code == 200
    assert get(client, service_id).status_code == 404
    assert locks(app) == []


def test_change_lock_released_on_failure(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changes that fail don't keep their lock (renames aren't blocked)."""

    def fail() -> None:
        raise RuntimeError("failure")

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "delete_one", "services", fail)
        response = _send(client, _changes(service_id)[2])
    assert fired
    assert response.status_code == 500
    assert locks(app) == []
    assert rename(client, service_id).status_code == 200


def test_stale_rename_blocks_changes(
    app: App, client: FlaskClient, service_id: str
) -> None:
    """Interrupted renames block changes until they are resumed."""
    make_lock(app, service_id)
    response = _send(client, _changes(service_id)[2])
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-busy"
    resume(app)
    assert _send(client, _changes(service_id)[2]).status_code == 200


@pytest.mark.parametrize("competitor", ["repeat", "other", "register"])
def test_target_taken_before_lock(
    app: App,
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    competitor: str,
) -> None:
    """Identifiers taken after the checks, before the locks, keep their roles.

    E.g., by a repeated request, a rename of another service to the same
    identifier, or an admin registering it (regression: the roles of the
    new identifier were removed).
    """
    other = register(client) if competitor == "other" else service_id
    moved = roles(client, other)
    competitors = {
        "repeat": lambda: rename(client, service_id),
        "other": lambda: rename(client, other),
        "register": lambda: client.put(
            f"{BASE}/services/{NEW_ID}", json=_other_service(), headers=headers(ADMIN)
        ),
    }
    responses: list[TestResponse] = []
    with monkeypatch.context() as patch:
        before_first_call(
            patch,
            "insert_one",
            locks_module.LOCKS,
            lambda: responses.append(competitors[competitor]()),
        )
        response = rename(client, service_id)
    assert responses[0].status_code in (200, 201)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-id-taken"
    assert roles(client, NEW_ID)
    if competitor != "register":
        assert roles(client, NEW_ID) == moved
    if competitor != "repeat":
        assert roles(client, service_id) == EXPECTED_ROLES
    assert locks(app) == []


def test_rename_deleted_before_lock(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Services deleted after the checks, before the locks, are not found."""
    responses: list[TestResponse] = []
    with monkeypatch.context() as patch:
        before_first_call(
            patch,
            "insert_one",
            locks_module.LOCKS,
            lambda: responses.append(_send(client, _changes(service_id)[2])),
        )
        response = rename(client, service_id)
    assert responses[0].status_code == 200
    assert response.status_code == 404
    assert get(client, NEW_ID).status_code == 404
    assert locks(app) == []


def test_rename_deleted_concurrently(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Services deleted before they move are not found; nothing is kept."""

    def delete() -> None:
        with app.app.app_context():
            get_collection("services").delete_one({"id": service_id})

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "update_one", "services", delete)
        assert rename(client, service_id).status_code == 404
    assert fired
    assert get(client, NEW_ID).status_code == 404
    assert locks(app) == []


def test_rename_taken_concurrently(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identifiers registered before the service moves are taken.

    E.g., by `POST /services`, which doesn't wait for renames.
    """

    def register_new() -> None:
        with app.app.app_context():
            get_collection("services").insert_one({"id": NEW_ID})

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "update_one", "services", register_new)
        response = rename(client, service_id)
    assert fired
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-id-taken"
    assert roles(client, service_id) == EXPECTED_ROLES
    assert locks(app) == []


def test_stale_roles_of_new_id_removed(
    app: App, client: FlaskClient, service_id: str
) -> None:
    """Roles left on the new identifier (e.g., of a deleted service) are removed."""
    with app.app.app_context():
        add_resource_role(BOB, "owner", f"{PATH}/{NEW_ID}")
    assert rename(client, service_id).status_code == 200
    assert roles(client, NEW_ID) == EXPECTED_ROLES


def test_failure_after_move_resumed(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that fails after the service moved is finished later.

    Until then, both identifiers stay locked; once the locks are stale, the
    new identifier gets all roles, the old one keeps none.
    """
    calls = 0
    original = rename_module.add_resource_role

    def fail_second(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("database unavailable")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(rename_module, "add_resource_role", fail_second)
        assert rename(client, service_id).status_code == 500
    assert get(client, service_id).status_code == 404
    assert get(client, NEW_ID).status_code == 200
    assert lock_ids(app) == sorted([service_id, NEW_ID], key=repr)
    response = rename(client, NEW_ID, new_id="OTHER")
    assert response.get_json()["code"] == "service-busy"
    resume(app)
    assert len(locks(app)) == 2
    with app.app.app_context():
        get_collection(locks_module.LOCKS).update_many(
            {}, {"$set": {"started_at": now() - STALE}}
        )
    resume(app)
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert locks(app) == []
    assert rename(client, NEW_ID, new_id="OTHER").status_code == 200


def test_failure_before_move_rolled_back(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that fails before the service moved is rolled back.

    The service keeps its identifier and roles; the locks are removed.
    """

    def fail() -> None:
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        before_first_call(patch, "update_one", "services", fail)
        assert rename(client, service_id).status_code == 500
    assert locks(app) == []
    assert roles(client, service_id) == EXPECTED_ROLES
    assert get(client, NEW_ID).status_code == 404


def test_invalid_role_not_moved(
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Roles that can't be granted (e.g., stored before FOCA rejected them) stay.

    The others move; a warning names the role.
    """
    original = rename_module.add_resource_role

    def reject_bob(subject: str, *args: Any, **kwargs: Any) -> bool:
        if subject == BOB:
            raise ValueError("invalid subject")
        return original(subject, *args, **kwargs)

    monkeypatch.setattr(rename_module, "add_resource_role", reject_bob)
    assert rename(client, service_id).status_code == 200
    assert roles(client, NEW_ID) == [
        role for role in EXPECTED_ROLES if role.get("user_id") != BOB
    ]
    assert (
        f"Role 'maintainer' of user '{BOB}' on service '{service_id}' not moved "
        f"to '{NEW_ID}': invalid subject" in caplog.text
    )


def test_resume_moved(app: App, client: FlaskClient, service_id: str) -> None:
    """Interrupted renames of moved services are finished."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    resume(app)
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert locks(app) == []


def test_resume_not_moved(app: App, client: FlaskClient, service_id: str) -> None:
    """Interrupted renames of services that didn't move are rolled back."""
    make_lock(app, service_id)
    resume(app)
    assert roles(client, service_id) == EXPECTED_ROLES
    assert get(client, NEW_ID).status_code == 404
    assert locks(app) == []


def test_resume_both_exist(
    app: App, client: FlaskClient, service_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Interrupted renames are rolled back if both identifiers are registered.

    Roles are not moved, as they might go to another service; this is logged.
    """
    make_lock(app, service_id)
    other = register(client, user=BOB)
    move_service(app, other, NEW_ID)
    resume(app)
    assert roles(client, service_id) == EXPECTED_ROLES
    assert locks(app) == []
    assert "check the roles of both" in caplog.text


def test_resume_both_deleted(app: App, client: FlaskClient, service_id: str) -> None:
    """Roles left on the old identifier are removed if neither is registered."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    with app.app.app_context():
        get_collection("services").delete_one({"id": NEW_ID})
    resume(app)
    assert locks(app) == []
    with app.app.app_context():
        assert list_roles(f"{PATH}/{service_id}") == []
        assert list_roles(f"{PATH}/{NEW_ID}") == []


def test_resume_recent_kept(app: App, client: FlaskClient, service_id: str) -> None:
    """Renames and changes that may still run (not stale) are left alone."""
    make_lock(app, service_id, age=timedelta(seconds=1))
    make_change_lock(app, "X", timedelta(seconds=1))
    resume(app)
    assert len(locks(app)) == 3


def test_resume_removes_stale_locks(app: App, client: FlaskClient) -> None:
    """Stale locks of changes, and of new identifiers alone, are removed."""
    make_change_lock(app, "X", STALE)
    with app.app.app_context():
        get_collection(locks_module.LOCKS).insert_one(
            {"_id": NEW_ID, "from": "X", "token": "b", "started_at": now() - STALE}
        )
    resume(app)
    assert locks(app) == []


def test_resume_claimed_once(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted renames are resumed once, also by concurrent calls."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    original = rename_module._claim

    def claimed_meanwhile(lock: dict[str, Any]) -> str | None:
        # another call claims the lock first
        assert original(lock) is not None
        return original(lock)

    finished: list[Any] = []
    monkeypatch.setattr(rename_module, "_claim", claimed_meanwhile)
    monkeypatch.setattr(rename_module, "_finish", lambda *args: finished.append(args))
    resume(app)
    assert finished == []
    assert len(locks(app)) == 2


def test_resume_failure_logged(
    app: App,
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Renames that fail to resume are logged and tried again; others resume."""
    other = register(client)
    make_lock(app, service_id)
    make_lock(app, other, "OTHER")
    move_service(app, service_id, NEW_ID)
    move_service(app, other, "OTHER")
    original = rename_module._move_roles

    def fail_first(path: str, source: str, target: str) -> None:
        if source == service_id:
            raise RuntimeError("database unavailable")
        original(path, source, target)

    with monkeypatch.context() as patch:
        patch.setattr(rename_module, "_move_roles", fail_first)
        resume(app)
    assert (
        f"Interrupted rename of service '{service_id}' to '{NEW_ID}' not resumed"
        in caplog.text
    )
    assert lock_ids(app) == sorted([service_id, NEW_ID], key=repr)
    assert roles(client, "OTHER") == [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"}
    ]


def test_resume_on_startup(app: App, client: FlaskClient, service_id: str) -> None:
    """The app resumes interrupted renames when it starts."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    restarted = create_app()
    assert roles(restarted.app.test_client(), NEW_ID) == EXPECTED_ROLES
    assert locks(restarted) == []


@pytest.mark.parametrize(
    "last, resumed",
    [(None, True), (time.monotonic, False)],
    ids=["due", "recent"],
)
def test_resume_before_requests(
    app: App,
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    last: Callable[[], float] | None,
    resumed: bool,
) -> None:
    """Requests resume interrupted renames, at most every few seconds."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    monkeypatch.setattr(rename_module, "RESUME_INTERVAL", 10.0)
    monkeypatch.setattr(rename_module, "_last_resume", None if last is None else last())
    assert client.get(f"{BASE}/services").status_code == 200
    assert (locks(app) == []) is resumed


def _unavailable(name: str) -> Any:
    """Fail like a database that can't be reached.

    Args:
        name: Name of the collection.

    Raises:
        pymongo.errors.ServerSelectionTimeoutError: Always.
    """
    raise ServerSelectionTimeoutError(f"no server for '{name}'")


def _resume_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the next request resume renames (unlike `_fast_locks()`).

    Requests resume at most once per interval, so call this after the
    requests that set up the test.

    Args:
        monkeypatch: Fixture to patch the interval and the last resume.
    """
    monkeypatch.setattr(rename_module, "RESUME_INTERVAL", 10.0)
    monkeypatch.setattr(rename_module, "_last_resume", None)


# Endpoints that aren't the registry's operations, by path (the Swagger UI,
# the spec, FOCA's service info and admin API); all others are
OTHER_PATHS = re.compile(
    rf"^({re.escape(BASE)}/(ui/|openapi\.|service-info$)|/admin/|/static/)"
)


def test_operation_endpoints(app: App) -> None:
    """All operations of the registry, and only those, resume renames."""
    rules = list(app.app.url_map.iter_rules())
    assert any(OTHER_PATHS.match(rule.rule) for rule in rules)
    for rule in rules:
        operation = rule.endpoint.rpartition(".")[2]
        is_operation = operation.startswith(rename_module.OPERATION_PREFIX)
        assert is_operation is not bool(OTHER_PATHS.match(rule.rule)), rule.rule


@pytest.mark.parametrize(
    "path",
    [
        f"{BASE}/ui/",
        f"{BASE}/openapi.json",
        f"{BASE}/openapi.yaml",
        f"{BASE}/service-info",
        "/admin/access-control/permissions",
        f"{BASE}/unknown",
    ],
    ids=["ui", "spec-json", "spec-yaml", "service-info", "admin-api", "not-found"],
)
def test_no_resume_before_other_requests(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The UI, spec, service info and admin API don't need the database."""
    make_lock(app, "SRV")
    _resume_due(monkeypatch)
    monkeypatch.setattr(rename_module, "get_collection", _unavailable)
    assert client.get(path).status_code < 500
    assert rename_module._last_resume is None


@pytest.mark.parametrize("fails", ["check", "resume"])
def test_resume_before_requests_unavailable(
    app: App,
    client: FlaskClient,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fails: str,
) -> None:
    """Requests continue if renames can't be resumed, which is tried again."""
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    _resume_due(monkeypatch)

    def fail_resume() -> None:
        raise AutoReconnect("connection lost")

    with monkeypatch.context() as patch:
        if fails == "check":
            patch.setattr(rename_module, "get_collection", _unavailable)
        else:
            patch.setattr(rename_module, "resume_renames", fail_resume)
        assert client.get(f"{BASE}/services").status_code == 200
    assert "Interrupted renames not checked; tried again later" in caplog.text
    assert len(locks(app)) == 2
    monkeypatch.setattr(rename_module, "_last_resume", time.monotonic() - 10.0)
    assert client.get(f"{BASE}/services").status_code == 200
    assert locks(app) == []


def test_resume_before_requests_timeout(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requests check for interrupted renames with a short timeout.

    Not resuming them, which takes several writes.
    """
    make_lock(app, service_id)
    timeouts: list[float | None] = []
    active: list[bool] = []
    original = pymongo.timeout

    @contextmanager
    def spy(seconds: float | None) -> Iterator[None]:
        timeouts.append(seconds)
        active.append(True)
        with original(seconds):
            yield
        active.pop()

    resumed: list[bool] = []
    monkeypatch.setattr(pymongo, "timeout", spy)
    monkeypatch.setattr(
        rename_module, "resume_renames", lambda: resumed.append(bool(active))
    )
    _resume_due(monkeypatch)
    assert client.get(f"{BASE}/services").status_code == 200
    assert timeouts == [rename_module.RESUME_CHECK_TIMEOUT]
    assert resumed == [False]


def test_no_resume_without_stale_locks(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running renames (recent locks) aren't resumed before requests."""
    make_lock(app, service_id, age=timedelta(0))
    _resume_due(monkeypatch)
    resumed: list[None] = []
    monkeypatch.setattr(rename_module, "resume_renames", lambda: resumed.append(None))
    assert client.get(f"{BASE}/services").status_code == 200
    assert resumed == []
    assert len(locks(app)) == 2
