"""Tests for renaming services, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`; the helpers for
roles are those of `tests/test_access_control.py`.
"""

from datetime import timedelta
from typing import Any
from urllib.parse import quote

from connexion import App
from flask.testing import FlaskClient
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
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
    headers,
    permission_list,
    register,
)

NEW_ID = "NEWSRV"
# Path of the services, under which roles are granted
PATH = f"{BASE}/services"


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
    """Read the locks of renames.

    Args:
        app: App.

    Returns:
        Stored locks, sorted by identifier.
    """
    with app.app.app_context():
        return list(get_collection(rename_module.RENAMES).find({}, sort=[("_id", 1)]))


def resume(app: App) -> None:
    """Finish or roll back all interrupted renames, as on startup.

    Args:
        app: App.
    """
    with app.app.test_request_context(f"{BASE}/services"):
        rename_module.resume_renames(min_age=timedelta(0))


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
    before = stored_service(app, service_id)
    assert before is not None
    response = rename(client, service_id)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == NEW_ID
    after = stored_service(app, NEW_ID)
    assert after is not None
    assert after["createdAt"] == before["createdAt"]
    assert after["updatedAt"] >= before["updatedAt"]
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


@pytest.mark.parametrize("new_id", [NEW_ID, None])
def test_rename_taken(client: FlaskClient, service_id: str, new_id: Any) -> None:
    """Identifiers of registered services, also its own, are taken (`409`)."""
    if new_id is None:
        new_id = service_id
    else:
        response = client.put(
            f"{BASE}/services/{new_id}",
            json={"name": "x"} | _other_service(),
            headers=headers(ADMIN),
        )
        assert response.status_code == 201, response.get_json()
    response = rename(client, service_id, new_id=new_id)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-id-taken"
    assert roles(client, service_id) == EXPECTED_ROLES


def _other_service() -> dict[str, Any]:
    """Get another service than those `register()` registers.

    Returns:
        Service with its own URL.
    """
    return {
        "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
        "organization": {"name": "Other", "url": "https://example.org"},
        "version": "1.0.0",
        "url": "https://other.example.org/ga4gh/tes/v1",
    }


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


@pytest.mark.parametrize("new_id", ["my tes", "a.b", "...", "Types", "#a", "\u00fc"])
def test_rename_special(client: FlaskClient, service_id: str, new_id: str) -> None:
    """Identifiers that clients can request (encoded) are accepted."""
    assert rename(client, service_id, new_id=new_id).status_code == 200
    encoded = quote(new_id, safe="")
    assert get(client, encoded).get_json()["id"] == new_id
    assert roles(client, encoded) == EXPECTED_ROLES


@pytest.mark.parametrize("locked", ["source", "target"])
def test_rename_in_progress(
    app: App, client: FlaskClient, service_id: str, locked: str
) -> None:
    """Services and identifiers of renames in progress can't be renamed (`409`).

    Neither as source nor as target, until the rename is finished.
    """
    other = register(client)
    with app.app.app_context():
        lock = (
            {"_id": service_id, "to": "X", "path": PATH, "started_at": now()}
            if locked == "source"
            else {"_id": NEW_ID, "from": "X", "started_at": now()}
        )
        get_collection(rename_module.RENAMES).insert_one(lock)
    source = service_id if locked == "source" else other
    response = rename(client, source)
    assert response.status_code == 409
    assert response.get_json()["code"] == "rename-in-progress"
    assert get(client, source).status_code == 200
    assert [lock["_id"] for lock in locks(app)] == [
        service_id if locked == "source" else NEW_ID
    ]


def test_rename_deleted_concurrently(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Services deleted before they are renamed are not found; nothing is kept."""

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
    """Identifiers registered before the service is renamed are taken."""

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
    stale = register(client, user=BOB)
    assert rename(client, stale, new_id=NEW_ID).status_code == 200
    response = client.delete(f"{BASE}/services/{NEW_ID}", headers=headers(BOB))
    assert response.status_code == 200
    # a role left on the identifier, as if revoking it had failed
    with app.app.test_request_context(f"{BASE}/services"):
        from foca.security.access_control import add_resource_role  # noqa: PLC0415

        add_resource_role(BOB, "owner", f"{BASE}/services/{NEW_ID}")
    assert rename(client, service_id).status_code == 200
    assert roles(client, NEW_ID) == EXPECTED_ROLES


def test_failure_after_move_resumed(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename that fails after the service moved is finished later.

    The new identifier then has all roles, the old one none; until then, the
    rename stays locked.
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
    assert len(locks(app)) == 2
    assert rename(client, NEW_ID, new_id="OTHER").get_json()["code"] == (
        "rename-in-progress"
    )
    resume(app)
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert locks(app) == []
    assert rename(client, NEW_ID, new_id="OTHER").status_code == 200


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


def _lock(app: App, source: str, target: str, age: timedelta) -> None:
    """Store the locks of a rename, as if it was interrupted.

    Args:
        app: App.
        source: Identifier of the renamed service.
        target: New identifier.
        age: How long ago the rename started.
    """
    started_at = now() - age
    with app.app.app_context():
        get_collection(rename_module.RENAMES).insert_many(
            [
                {"_id": source, "to": target, "path": PATH, "started_at": started_at},
                {"_id": target, "from": source, "started_at": started_at},
            ]
        )


def _move(app: App, source: str, target: str) -> None:
    """Change the identifier of a stored service, as a rename does first.

    Args:
        app: App.
        source: Identifier of the service.
        target: New identifier.
    """
    with app.app.app_context():
        get_collection("services").update_one({"id": source}, {"$set": {"id": target}})


def test_resume_moved(app: App, client: FlaskClient, service_id: str) -> None:
    """Interrupted renames of moved services are finished."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    _move(app, service_id, NEW_ID)
    resume(app)
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert locks(app) == []


def test_resume_not_moved(app: App, client: FlaskClient, service_id: str) -> None:
    """Interrupted renames of services that didn't move are rolled back."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
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
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    other = register(client, user=BOB)
    _move(app, other, NEW_ID)
    resume(app)
    assert roles(client, service_id) == EXPECTED_ROLES
    assert locks(app) == []
    assert "check the roles of both" in caplog.text


def test_resume_both_deleted(app: App, client: FlaskClient, service_id: str) -> None:
    """Roles left on the old identifier are removed if neither is registered."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    _move(app, service_id, NEW_ID)
    with app.app.app_context():
        get_collection("services").delete_one({"id": NEW_ID})
    resume(app)
    assert locks(app) == []
    with app.app.test_request_context(f"{BASE}/services"):
        from foca.security.access_control import list_roles  # noqa: PLC0415

        assert list_roles(f"{BASE}/services/{service_id}") == []
        assert list_roles(f"{BASE}/services/{NEW_ID}") == []


def test_resume_on_startup(app: App, client: FlaskClient, service_id: str) -> None:
    """The app resumes interrupted renames when it starts."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    _move(app, service_id, NEW_ID)
    restarted = create_app()
    assert roles(restarted.app.test_client(), NEW_ID) == EXPECTED_ROLES
    assert locks(restarted) == []


def test_resume_recent_kept(app: App, client: FlaskClient, service_id: str) -> None:
    """Renames that may still run (started recently) are left alone."""
    _lock(app, service_id, NEW_ID, timedelta(seconds=1))
    with app.app.test_request_context(f"{BASE}/services"):
        rename_module.resume_renames()
    assert len(locks(app)) == 2


def test_resume_orphaned_target(app: App, client: FlaskClient) -> None:
    """Locks of new identifiers without a lock of their rename are removed."""
    with app.app.app_context():
        get_collection(rename_module.RENAMES).insert_one(
            {"_id": NEW_ID, "from": "X", "started_at": now() - timedelta(minutes=5)}
        )
    resume(app)
    assert locks(app) == []


def test_resume_claimed_once(
    app: App, client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted renames are resumed once, also by concurrent calls."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    _move(app, service_id, NEW_ID)
    original = rename_module._claim

    def claimed_meanwhile(lock: dict[str, Any]) -> bool:
        # another call claims the lock first
        assert original(lock)
        return original(lock)

    monkeypatch.setattr(rename_module, "_claim", claimed_meanwhile)
    resume(app)
    assert len(locks(app)) == 2


def test_stale_renames_resumed_on_rename(
    app: App, client: FlaskClient, service_id: str
) -> None:
    """Renames resume interrupted ones first, so a retry succeeds."""
    _lock(app, service_id, NEW_ID, timedelta(minutes=5))
    assert rename(client, service_id).status_code == 200
    assert roles(client, NEW_ID) == EXPECTED_ROLES
    assert locks(app) == []
