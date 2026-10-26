"""Tests for access control, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`. Some tests patch
internals of FOCA 0.17's ownership helpers (`ownership_module`) to simulate
failures and concurrent requests that its public API can't produce; adapt
them when upgrading FOCA.
"""

from collections.abc import Callable
from copy import deepcopy
from itertools import count
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from connexion import App
from flask.testing import FlaskClient
from foca import get_config
from foca.security.access_control import (
    add_resource_role,
    is_admin,
    list_resource_roles,
)
from foca.security.access_control import (
    ownership as ownership_module,
)
from foca.security.access_control.enforcement import get_enforcer
from foca.security.access_control.foca_casbin_adapter.adapter import Adapter
from pymongo.collection import Collection
import pytest
import yaml

import cloud_registry.app
from cloud_registry.exceptions import Unauthorized
from cloud_registry.ga4gh.registry import permissions as permissions_module
from cloud_registry.ga4gh.registry import server as server_module
from cloud_registry.ga4gh.registry.permissions import may_read_service
from cloud_registry.ga4gh.registry.service import DuplicateService
from cloud_registry.utils import get_collection
from tests.conftest import (
    ADMIN,
    COLLECTION_CLASS,
    CONFIG_OVERLAY,
    GROUPS,
    NAMES,
    TEAM_GROUP,
    USER,
    make_headers,
)

BASE = "/ga4gh/registry/v1"
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
# Users: alice (owner of services she registers, member of the team group),
# bob (member of another group), carol (no name in user info), dave (blank
# name), erin (member of the team group), and an admin
ALICE, BOB, CAROL = USER, "bob@example.org", "carol@example.org"
DAVE, ERIN = "dave@example.org", "erin@example.org"
# Group of bob, which alice is not a member of
OTHER_GROUP = GROUPS[BOB][0]


def headers(user: str | None = None) -> dict[str, str]:
    """Get request headers with an access token for a user.

    Args:
        user: User identifier; no token if `None`.

    Returns:
        Request headers.
    """
    return {} if user is None else make_headers(user)


# Numbers for the URLs of registered services, which must be unique
URLS = count()


def register(client: FlaskClient, user: str = ALICE) -> str:
    """Register a service (with a new URL) as a user.

    Args:
        client: Test client of the app.
        user: User who registers the service.

    Returns:
        Identifier of the registered service.
    """
    service = {**_service(), "url": f"https://tes{next(URLS)}.example.org"}
    response = client.post(f"{BASE}/services", json=service, headers=headers(user))
    assert response.status_code == 201, response.get_json()
    service_id: str = response.get_json()
    return service_id


def _service() -> dict[str, Any]:
    """Get a copy of the test service.

    Returns:
        The test service, without identifier.
    """
    return deepcopy(SERVICE)


def permission_list(
    client: FlaskClient, service_id: str, user: str = ADMIN
) -> list[dict[str, Any]]:
    """Get the roles granted on a service, as listed (users and groups).

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who lists the roles.

    Returns:
        Roles on the service.
    """
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(user)
    )
    assert response.status_code == 200, response.get_json()
    roles: list[dict[str, Any]] = response.get_json()
    return roles


def permissions(
    client: FlaskClient, service_id: str, user: str = ADMIN
) -> list[tuple[str, str]]:
    """Get the roles granted to users on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who lists the roles.

    Returns:
        User identifiers and roles, sorted.
    """
    return sorted(
        (p["user_id"], p["role"])
        for p in permission_list(client, service_id, user)
        if "user_id" in p
    )


def group_permissions(
    client: FlaskClient, service_id: str, user: str = ADMIN
) -> list[tuple[str, str]]:
    """Get the roles granted to groups on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who lists the roles.

    Returns:
        Groups and roles, sorted.
    """
    return sorted(
        (p["group"], p["role"])
        for p in permission_list(client, service_id, user)
        if "group" in p
    )


def grant(
    client: FlaskClient, service_id: str, user: str, role: str, as_user: str = ALICE
) -> int:
    """Grant a user a role on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who gets the role.
        role: Role to grant.
        as_user: User who grants the role.

    Returns:
        Status code of the response.
    """
    return client.put(
        f"{BASE}/services/{service_id}/permissions/{user}",
        json={"role": role},
        headers=headers(as_user),
    ).status_code


def update(client: FlaskClient, service_id: str, user: str) -> int:
    """Replace a service with the test service, as a user.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who replaces the service.

    Returns:
        Status code of the response.
    """
    return client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=headers(user)
    ).status_code


def delete(client: FlaskClient, service_id: str, user: str) -> int:
    """Delete a service, as a user.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who deletes the service.

    Returns:
        Status code of the response.
    """
    return client.delete(
        f"{BASE}/services/{service_id}", headers=headers(user)
    ).status_code


def test_public_read(client: FlaskClient) -> None:
    """Anyone can read services, service types and service info."""
    service_id = register(client)
    for path in [
        "/services",
        f"/services/{service_id}",
        "/services/types",
        "/service-info",
    ]:
        response = client.get(f"{BASE}{path}")
        assert response.status_code == 200, path
    assert client.get(f"{BASE}/services/UNKNWN").status_code == 404


def test_write_requires_authentication(client: FlaskClient) -> None:
    """Writing requires a token."""
    service_id = register(client)
    assert client.post(f"{BASE}/services", json=_service()).status_code == 401
    assert client.put(f"{BASE}/services/X", json=_service()).status_code == 401
    assert client.delete(f"{BASE}/services/{service_id}").status_code == 401
    response = client.get(f"{BASE}/services/{service_id}/permissions")
    assert response.status_code == 401
    assert client.get(f"{BASE}/me").status_code == 401


def test_creator_is_owner(client: FlaskClient) -> None:
    """Any authenticated user can register a service and becomes its owner."""
    service_id = register(client, user=BOB)
    assert permissions(client, service_id) == [(BOB, "owner")]
    assert update(client, service_id, BOB) == 200
    assert delete(client, service_id, BOB) == 200


def test_others_forbidden(client: FlaskClient) -> None:
    """Other users can neither modify a service nor its permissions."""
    service_id = register(client)
    assert update(client, service_id, BOB) == 403
    assert delete(client, service_id, BOB) == 403
    assert grant(client, service_id, BOB, "owner", as_user=BOB) == 403
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(BOB)
    )
    assert response.status_code == 403
    assert client.get(f"{BASE}/services/{service_id}").status_code == 200


def test_admin(client: FlaskClient) -> None:
    """Admins (via a group from user info) can modify anything."""
    service_id = register(client)
    assert update(client, service_id, ADMIN) == 200
    assert grant(client, service_id, CAROL, "owner", ADMIN) == 200
    assert delete(client, service_id, ADMIN) == 200


def test_maintainer(client: FlaskClient) -> None:
    """Maintainers can update, but not delete a service or manage access."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert update(client, service_id, BOB) == 200
    assert delete(client, service_id, BOB) == 403
    assert grant(client, service_id, BOB, "owner", as_user=BOB) == 403
    # updating does not make maintainers owners
    assert permissions(client, service_id) == [
        (ALICE, "owner"),
        (BOB, "maintainer"),
    ]


def test_owners_manage_permissions(client: FlaskClient) -> None:
    """Owners can grant, change and revoke roles of other users."""
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    assert grant(client, service_id, BOB, "maintainer", as_user=CAROL) == 200
    # granting a role replaces other roles
    assert grant(client, service_id, BOB, "owner") == 200
    assert permissions(client, service_id, user=CAROL) == [
        (ALICE, "owner"),
        (BOB, "owner"),
        (CAROL, "owner"),
    ]
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/{BOB}",
        headers=headers(ALICE),
    )
    assert response.status_code == 200
    assert update(client, service_id, BOB) == 403
    assert delete(client, service_id, CAROL) == 200


def test_invalid_permissions(client: FlaskClient) -> None:
    """Invalid roles, unknown services and missing roles are rejected."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "admin") == 400
    assert grant(client, service_id, "bob,x", "owner") == 400
    assert grant(client, "UNKNWN", BOB, "owner", ADMIN) == 404
    assert revoke(client, "UNKNWN", BOB, as_user=ADMIN) == 404
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/{BOB}",
        headers=headers(ALICE),
    )
    assert response.status_code == 404


def test_put_create(client: FlaskClient) -> None:
    """Only admins can register services with chosen identifiers."""
    assert update(client, "MYSERV", ALICE) == 403
    assert update(client, "MYSERV", ADMIN) == 201
    assert permissions(client, "MYSERV") == [(ADMIN, "owner")]


def test_permissions_removed_with_service(client: FlaskClient) -> None:
    """Roles on a service are revoked when it is deleted."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert delete(client, service_id, ALICE) == 200
    # a new service with the same identifier has none of the old roles
    assert update(client, service_id, ADMIN) == 201
    assert permissions(client, service_id) == [(ADMIN, "owner")]
    assert update(client, service_id, BOB) == 403


def revoke(
    client: FlaskClient, service_id: str, user: str, as_user: str = ALICE
) -> int:
    """Revoke the roles of a user on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User whose roles are revoked.
        as_user: User who revokes the roles.

    Returns:
        Status code of the response.
    """
    return client.delete(
        f"{BASE}/services/{service_id}/permissions/{user}", headers=headers(as_user)
    ).status_code


def me(client: FlaskClient, user: str) -> dict[str, Any]:
    """Get the current user (`GET /me`), as a user.

    Args:
        client: Test client of the app.
        user: User who sends the request.

    Returns:
        The user's identifier, admin status and roles on services.
    """
    response = client.get(f"{BASE}/me", headers=headers(user))
    assert response.status_code == 200, response.get_json()
    current: dict[str, Any] = response.get_json()
    return current


def test_me(client: FlaskClient) -> None:
    """Users get their identifier, admin status and roles on services."""
    first, second = register(client), register(client)
    assert grant(client, first, BOB, "maintainer") == 200
    assert me(client, ALICE) == {
        "user_id": ALICE,
        "admin": False,
        "may_add_service_types": True,
        "service_types_pending": False,
        "services": sorted(
            [
                {"service_id": first, "role": "owner"},
                {"service_id": second, "role": "owner"},
            ],
            key=lambda service: service["service_id"],
        ),
    }
    assert me(client, BOB)["services"] == [{"service_id": first, "role": "maintainer"}]
    assert me(client, CAROL)["services"] == []
    admin = me(client, ADMIN)
    assert (admin["admin"], admin["services"]) == (True, [])
    assert delete(client, first, ALICE) == 200
    assert me(client, BOB)["services"] == []


def _names(client: FlaskClient, service_id: str) -> dict[str, str | None]:
    """Get the display names of the users with roles on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.

    Returns:
        Display names by user identifier; `None` for unknown names.
    """
    response = client.get(
        f"{BASE}/services/{service_id}/permissions",
        headers=headers(ADMIN),
    )
    return {p["user_id"]: p.get("name") for p in response.get_json() if "user_id" in p}


def test_permission_names(client: FlaskClient) -> None:
    """Display names are shown once users have used the registry."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert grant(client, service_id, CAROL, "maintainer") == 200
    assert grant(client, service_id, DAVE, "maintainer") == 200
    # registering stores the owner's name; others are unknown so far
    unknown = {BOB: None, CAROL: None, DAVE: None}
    assert _names(client, service_id) == {ALICE: NAMES[ALICE], **unknown}
    me(client, BOB)
    me(client, CAROL)  # the identity provider reports no name
    me(client, DAVE)
    assert _names(client, service_id) == {
        ALICE: NAMES[ALICE],
        BOB: NAMES[BOB],
        CAROL: None,
        DAVE: None,
    }


def test_last_owner(client: FlaskClient) -> None:
    """The last owner of a service cannot be removed or downgraded.

    Not even by admins.
    """
    service_id = register(client)
    assert revoke(client, service_id, ALICE) == 409
    assert grant(client, service_id, ALICE, "maintainer") == 409
    assert revoke(client, service_id, ALICE, as_user=ADMIN) == 409
    assert grant(client, service_id, ALICE, "maintainer", as_user=ADMIN) == 409
    assert grant(client, service_id, ALICE, "owner") == 200
    # with another owner, the first owner can step down
    assert grant(client, service_id, CAROL, "owner") == 200
    assert grant(client, service_id, ALICE, "maintainer") == 200
    assert revoke(client, service_id, ALICE, as_user=CAROL) == 200
    assert revoke(client, service_id, CAROL, as_user=CAROL) == 409
    assert permissions(client, service_id) == [(CAROL, "owner")]


def test_names_per_service(client: FlaskClient) -> None:
    """Names are only shown for users who used the registry with their role.

    The role must be on the service, and names are removed with the role.
    """
    me(client, BOB)  # before being granted a role
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert _names(client, service_id)[BOB] is None
    me(client, BOB)
    assert _names(client, service_id)[BOB] == NAMES[BOB]
    assert revoke(client, service_id, BOB) == 200
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert _names(client, service_id)[BOB] is None


@pytest.mark.parametrize("change", ["revoke", "downgrade"])
def test_last_owner_restored(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """If concurrent changes removed all owners, the removed owner is restored."""
    service_id = register(client)
    # skip the check before the change, so that only the check afterwards can
    # keep the service from losing its last owner (as when concurrent
    # requests removed the other owners after the first check)
    monkeypatch.setattr(ownership_module, "_check_other_owners", lambda *args: None)
    if change == "revoke":
        assert revoke(client, service_id, ALICE) == 409
    else:
        assert grant(client, service_id, ALICE, "maintainer") == 409
    assert permissions(client, service_id) == [(ALICE, "owner")]


def test_stale_roles_cleared(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles left on the identifier of a deleted service don't apply to a new one.

    The new service has that identifier.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "owner") == 200
    # as if revoking the roles of the deleted service had failed
    with monkeypatch.context() as patch:
        patch.setattr(server_module, "remove_permissions", lambda serviceId: None)
        assert delete(client, service_id, ALICE) == 200
    assert update(client, service_id, ADMIN) == 201
    assert permissions(client, service_id) == [(ADMIN, "owner")]


def test_invalid_identifier_rolled_back(client: FlaskClient) -> None:
    """Services whose owner can't be stored are not registered."""
    response = client.put(
        f"{BASE}/services/a,b", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 400
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services/a,b").status_code == 404


def test_stale_name_not_shown(app: App, client: FlaskClient) -> None:
    """A name stored before a role is granted is not shown with the role."""
    service_id = register(client)
    with app.app.app_context():
        get_collection("user_names").insert_one(
            {"service_id": service_id, "user_id": BOB, "name": "Stale"}
        )
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert _names(client, service_id)[BOB] is None


def test_owner_failure_rolled_back(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Services whose owner can't be stored for other reasons aren't registered."""

    def fail(serviceId: str) -> None:
        """Fail to store the owner."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(server_module, "add_owner", fail)
    response = client.put(
        f"{BASE}/services/MYSERV", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 500
    assert client.get(f"{BASE}/services/MYSERV").status_code == 404


def test_failed_grant_keeps_rights(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If granting a role fails, the user keeps their other roles.

    FOCA 0.17 stores the new role before it removes the others, so a failure
    to store it changes nothing.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "owner") == 200

    def fail(**kwargs: Any) -> bool:
        """Fail to grant the role."""
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(ownership_module, "_replace_resource_role", fail)
        assert grant(client, service_id, BOB, "maintainer") == 500
    assert sorted(permissions(client, service_id)) == [(ALICE, "owner"), (BOB, "owner")]


def test_failed_grant_cleanup_keeps_rights(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If removing the other roles fails, the new role is removed again.

    FOCA's adapter stores the new role first; when removing the subject's
    other roles fails, it removes the new one, so that a failed grant never
    leaves the subject with more rights.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200

    def fail(*args: Any, **kwargs: Any) -> None:
        """Fail to remove the other roles."""
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(Adapter, "_keep_latest_role", fail)
        assert grant(client, service_id, BOB, "owner") == 500
    assert sorted(permissions(client, service_id)) == [
        (ALICE, "owner"),
        (BOB, "maintainer"),
    ]


def test_permissions_of_unknown_service(client: FlaskClient) -> None:
    """Permissions of unknown services are not found."""
    response = client.get(f"{BASE}/services/UNKNWN/permissions", headers=headers(ADMIN))
    assert response.status_code == 404


def test_revoke_invalid_user_id(client: FlaskClient) -> None:
    """Roles of invalid user identifiers are not found."""
    service_id = register(client)
    assert revoke(client, service_id, "bob,x") == 404


def test_roles_of_invalid_service_id(app: App, client: FlaskClient) -> None:
    """Services whose identifiers can't be in policy rules have no roles.

    E.g., services stored with commas in their identifiers before these were
    checked: granting roles is rejected (`400`), revoking them not found.
    """
    with app.app.app_context():
        get_collection("services").insert_one({**_service(), "id": "a,b"})
    assert grant(client, "a,b", BOB, "owner", as_user=ADMIN) == 400
    assert revoke(client, "a,b", BOB, as_user=ADMIN) == 404
    assert permission_list(client, "a,b") == []


def test_maintainer_needs_owner(app: App, client: FlaskClient) -> None:
    """Roles other than `owner` are only granted on services with an owner.

    E.g., on services stored before owners were introduced, admins grant
    `owner` first (`400` otherwise).
    """
    service_id = register(client)
    with app.app.test_request_context(f"{BASE}/services/{service_id}"):
        app.app.preprocess_request()
        permissions_module.remove_permissions(service_id)
    assert grant(client, service_id, BOB, "maintainer", as_user=ADMIN) == 400
    assert permission_list(client, service_id) == []
    assert grant(client, service_id, CAROL, "owner", as_user=ADMIN) == 200
    assert grant(client, service_id, BOB, "maintainer", as_user=ADMIN) == 200


def test_group_roles_need_group_subjects(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles are not granted to groups if they wouldn't apply (`400`).

    That is, if groups are mapped (`group_mapping`), but not kept as subjects
    (`keep_group_subjects`).
    """
    service_id = register(client)
    with app.app.app_context():
        conf = get_config().security.access_control
    monkeypatch.setattr(conf, "keep_group_subjects", False)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 400
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer", ADMIN) == 400
    assert group_permissions(client, service_id) == []


def test_owner_not_restored_on_deleted_service(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No owner is restored for a service deleted concurrently."""
    service_id = register(client)
    calls = []

    def exists_once(serviceId: str) -> bool:
        """Report that the service exists for the first check only."""
        calls.append(serviceId)
        return len(calls) == 1

    with monkeypatch.context() as patch:
        patch.setattr(ownership_module, "_check_other_owners", lambda *args: None)
        patch.setattr(permissions_module, "service_exists", exists_once)
        assert revoke(client, service_id, ALICE) == 404
    assert permissions(client, service_id) == []


def test_no_claims_without_token(app: App) -> None:
    """Without a validated token, there are no claims."""
    with app.app.test_request_context():
        assert permissions_module._get_current_claims() == {}


@pytest.mark.parametrize(
    "operation",
    [
        lambda: permissions_module.add_owner("ABC"),
        lambda: permissions_module.remove_owner("ABC"),
        permissions_module.getMe,
    ],
    ids=["add_owner", "remove_owner", "getMe"],
)
def test_subject_required(app: App, operation: Callable[[], object]) -> None:
    """Operations that need a subject reject requests without one."""
    with app.app.test_request_context(), pytest.raises(Unauthorized):
        operation()


def test_is_admin_without_subject(app: App) -> None:
    """Requests without a subject or groups aren't an admin's.

    Also if a user named `None` is an admin.
    """
    with app.app.test_request_context(f"{BASE}/me"):
        app.app.preprocess_request()
        prefix = get_config().security.access_control.subject_prefix
        get_enforcer().add_role_for_user(f"{prefix}None", "admin")
        assert not is_admin()


def test_api_base_path_outside_api(app: App) -> None:
    """Requests not handled by an API have no API base path."""
    with (
        app.app.test_request_context("/unknown"),
        pytest.raises(RuntimeError, match="not handled by an API"),
    ):
        permissions_module._api_base_path()


def _roles(app: App, service_id: str) -> list[tuple[str, str]]:
    """Get the roles granted on a service, also if it doesn't exist.

    Args:
        app: Connexion app.
        service_id: Service identifier.

    Returns:
        Subjects and roles, sorted.
    """
    with app.app.test_request_context(f"{BASE}/services/{service_id}"):
        app.app.preprocess_request()
        return sorted(list_resource_roles(f"{BASE}/services/{service_id}"))


def before_first_call(
    patch: pytest.MonkeyPatch,
    method: str,
    collection: str,
    callback: Callable[[], None],
) -> list[bool]:
    """Run a callback once, before the first call of a collection method.

    The method is one of the collections of the tests (`COLLECTION_CLASS`),
    and the callback runs before its first call on a given collection (e.g.,
    to interleave another request).

    Args:
        patch: Monkeypatch (context) to patch the method with.
        method: Name of the method of `COLLECTION_CLASS`.
        collection: Name of the collection.
        callback: Function to call, without arguments.

    Returns:
        List that is non-empty once the callback ran.
    """
    original = getattr(COLLECTION_CLASS, method)
    fired: list[bool] = []

    def wrapper(self: Collection[Any], *args: Any, **kwargs: Any) -> Any:
        """Run the callback before the first call on the collection."""
        if self.name == collection and not fired:
            fired.append(True)
            callback()
        return original(self, *args, **kwargs)

    patch.setattr(COLLECTION_CLASS, method, wrapper)
    return fired


@pytest.mark.parametrize(
    ("user", "status", "roles"),
    [(BOB, 404, []), (ADMIN, 201, [(ADMIN, "owner")])],
    ids=["maintainer", "admin"],
)
def test_put_racing_delete(
    app: App,
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    user: str,
    status: int,
    roles: list[tuple[str, str]],
) -> None:
    """A service deleted while it is replaced is only registered again by admins.

    Others get `404`, and don't become owners.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200

    def delete_service() -> None:
        """Delete the service, as the owner after it was read for the update."""
        permissions_module.remove_permissions(service_id)
        get_collection("services").delete_one({"id": service_id})

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "replace_one", "services", delete_service)
        assert update(client, service_id, user) == status
    assert fired
    assert _roles(app, service_id) == roles
    expected = 404 if status == 404 else 200
    assert client.get(f"{BASE}/services/{service_id}").status_code == expected


def test_create_racing_delete(
    app: App,
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The creator's owner role on a service deleted meanwhile is revoked again.

    The owner role (and name) of the creator of a new service deleted while
    its owner is stored is revoked (`404`); roles granted by others on the
    identifier (e.g., to the owner of a service registered again with it) are
    kept.
    """

    def delete_service() -> None:
        """Delete the service, and grant a role on its identifier.

        The service is deleted before its owner role is checked (its roles
        were revoked before the owner role was granted), and another request
        grants a role on the identifier.
        """
        get_collection("services").delete_one({"id": "MYSERV"})
        add_resource_role(
            subject=BOB,
            role="owner",
            resource=permissions_module.service_path("MYSERV"),
        )

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "update_one", "user_names", delete_service)
        assert update(client, "MYSERV", ADMIN) == 404
    assert fired
    assert any("revoked from its creator" in msg for msg in caplog.messages)
    assert _roles(app, "MYSERV") == [(BOB, "owner")]
    with app.app.app_context():
        names = get_collection("user_names")
        assert names.count_documents({"service_id": "MYSERV"}) == 0
    assert client.get(f"{BASE}/services/MYSERV").status_code == 404


def test_put_stale_role(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles left on the identifier of a deleted service don't allow registering.

    Users other than admins can't register the service again with them.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    # as if revoking the roles of the deleted service had failed
    with monkeypatch.context() as patch:
        patch.setattr(server_module, "remove_permissions", lambda serviceId: None)
        assert delete(client, service_id, ALICE) == 200
    assert update(client, service_id, BOB) == 404
    assert client.get(f"{BASE}/services/{service_id}").status_code == 404


def test_roles_removed_before_delete(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles on a service are revoked before it is deleted.

    So no role is left on a deleted service if deleting it fails.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200

    def fail(self: Collection[Any], *args: Any, **kwargs: Any) -> None:
        """Fail to delete the service."""
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(COLLECTION_CLASS, "delete_one", fail)
        assert delete(client, service_id, ALICE) == 500
    assert _roles(app, service_id) == []
    assert client.get(f"{BASE}/services/{service_id}").status_code == 200


def delete_after_first_check(patch: pytest.MonkeyPatch, service_id: str) -> list[bool]:
    """Delete a service after its first existence check.

    Patches the app's existence check (`service_exists()`), which it also
    passes to FOCA's helpers as `exists`, so that the service is deleted
    before the second check, i.e., after a role on it was granted. Only the
    service is deleted, not its roles (as if the deletion's second
    revocation had run before the role was granted), so that the role is
    only revoked if FOCA's helper revokes it.

    Args:
        patch: Monkeypatch (context) to patch the check with.
        service_id: Service identifier.

    Returns:
        List that is non-empty once the service was deleted.
    """
    service_exists = permissions_module.service_exists
    deleted: list[bool] = []
    calls = count()

    def check(serviceId: str) -> bool:
        """Check whether the service exists, deleting it on the second call."""
        if next(calls) == 1:
            deleted.append(True)
            get_collection("services").delete_one({"id": service_id})
        return service_exists(serviceId)

    patch.setattr(permissions_module, "service_exists", check)
    return deleted


def test_grant_racing_delete(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role granted on a service deleted meanwhile is revoked again (`404`)."""
    service_id = register(client)
    with monkeypatch.context() as patch:
        deleted = delete_after_first_check(patch, service_id)
        assert grant(client, service_id, BOB, "maintainer") == 404
    assert deleted == [True]
    assert _roles(app, service_id) == [(ALICE, "owner")]


def test_delete_racing_grant(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Roles granted while a service is deleted are revoked with it.

    The roles are granted after the service's roles were revoked.
    """
    service_id = register(client)

    def grant_role() -> None:
        """Grant a role after the roles were revoked.

        The grant's check found the service still registered.
        """
        add_resource_role(
            subject=BOB,
            role="owner",
            resource=permissions_module.service_path(service_id),
        )

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "delete_one", "services", grant_role)
        assert delete(client, service_id, ALICE) == 200
    assert fired
    assert _roles(app, service_id) == []


PERMISSIONS_API = "/admin/access-control/permissions"


def test_permissions_api_admin_only(client: FlaskClient) -> None:
    """FOCA's permissions API is for admins only."""
    assert client.get(PERMISSIONS_API).status_code == 401
    assert client.get(PERMISSIONS_API, headers=headers(ALICE)).status_code == 403
    rule = {"policy_type": "g", "rule": {"v0": f"user:{ALICE}", "v1": "admin"}}
    response = client.post(PERMISSIONS_API, json=rule, headers=headers(ALICE))
    assert response.status_code == 403
    assert me(client, ALICE)["admin"] is False
    response = client.get(PERMISSIONS_API, headers=headers(ADMIN))
    assert response.status_code == 200
    # the configured policies are listed
    assert response.get_json()["pagination"]["total"] > 0


def test_admin_via_permissions_api(client: FlaskClient) -> None:
    """Users granted the role `admin` via the permissions API are admins.

    `GET /me` says so, until the role is revoked.
    """
    service_id = register(client)
    assert me(client, CAROL)["admin"] is False
    rule = {"policy_type": "g", "rule": {"v0": f"user:{CAROL}", "v1": "admin"}}
    response = client.post(PERMISSIONS_API, json=rule, headers=headers(ADMIN))
    assert response.status_code == 200
    rule_id = response.get_json()
    assert me(client, CAROL)["admin"] is True
    assert update(client, service_id, CAROL) == 200
    response = client.delete(f"{PERMISSIONS_API}/{rule_id}", headers=headers(ADMIN))
    assert response.status_code == 200
    assert me(client, CAROL)["admin"] is False
    assert update(client, service_id, CAROL) == 403


# Loggers of role changes: FOCA's ownership helpers, and the app (deletions)
ROLE_LOGGERS = (ownership_module.__name__, permissions_module.__name__)


def _role_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Get the logged messages about roles, by FOCA's helpers and the app.

    Args:
        caplog: Fixture capturing the log records, at level `INFO` (set by the
            tests).

    Returns:
        Messages, in the order they were logged.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name in ROLE_LOGGERS and record.levelname == "INFO"
    ]


def test_role_changes_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Changes of roles are logged.

    The messages say who changed which role of whom on which service.
    """
    caplog.set_level("INFO")
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert grant(client, service_id, BOB, "owner") == 200
    assert revoke(client, service_id, BOB) == 200
    assert delete(client, service_id, ADMIN) == 200
    resource = f"{BASE}/services/{service_id}"
    assert _role_messages(caplog) == [
        f"Role 'owner' on '{resource}' granted to user '{ALICE}' by user '{ALICE}'",
        f"Role 'maintainer' on '{resource}' granted to user '{BOB}' by user "
        f"'{ALICE}' (previous roles: none)",
        f"Role 'owner' on '{resource}' granted to user '{BOB}' by user "
        f"'{ALICE}' (previous roles: maintainer)",
        f"Roles on '{resource}' of user '{BOB}' revoked by user '{ALICE}' "
        "(previous roles: owner)",
        f"All roles on service '{service_id}' revoked by '{ADMIN}'.",
    ]
    assert f"Service '{service_id}' deleted by '{ADMIN}'." in caplog.text


def group_path(service_id: str, group: str) -> str:
    """Get the path of the roles of a group on a service.

    Args:
        service_id: Service identifier.
        group: Group, URL-encoded in the path.

    Returns:
        Path of the group's roles.
    """
    return f"{BASE}/services/{service_id}/permissions/groups/{quote(group, safe='')}"


def grant_group(
    client: FlaskClient, service_id: str, group: str, role: str, as_user: str = ALICE
) -> int:
    """Grant a group a role on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        group: Group that gets the role.
        role: Role to grant.
        as_user: User who grants the role.

    Returns:
        Status code of the response.
    """
    return client.put(
        group_path(service_id, group), json={"role": role}, headers=headers(as_user)
    ).status_code


def revoke_group(
    client: FlaskClient, service_id: str, group: str, as_user: str = ALICE
) -> int:
    """Revoke the roles of a group on a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        group: Group whose roles are revoked.
        as_user: User who revokes the roles.

    Returns:
        Status code of the response.
    """
    return client.delete(
        group_path(service_id, group), headers=headers(as_user)
    ).status_code


def test_group_roles(client: FlaskClient) -> None:
    """Owners can grant roles to groups they are a member of.

    All members of the group then have the role.
    """
    service_id = register(client)
    response = client.put(
        group_path(service_id, TEAM_GROUP),
        json={"role": "owner"},
        headers=headers(ALICE),
    )
    assert response.status_code == 200
    assert response.get_json() == {"group": TEAM_GROUP, "role": "owner"}
    # user and group roles are listed together, and distinguishable
    assert permission_list(client, service_id) == [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"},
        {"group": TEAM_GROUP, "role": "owner"},
    ]
    # members of an owner group are owners; others are not
    assert update(client, service_id, ERIN) == 200
    assert group_permissions(client, service_id, user=ERIN) == [(TEAM_GROUP, "owner")]
    assert grant(client, service_id, CAROL, "maintainer", as_user=ERIN) == 200
    assert update(client, service_id, BOB) == 403
    # members of a maintainer group can update only
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer") == 200
    assert group_permissions(client, service_id) == [(TEAM_GROUP, "maintainer")]
    assert update(client, service_id, ERIN) == 200
    assert delete(client, service_id, ERIN) == 403
    assert grant(client, service_id, BOB, "maintainer", as_user=ERIN) == 403
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(ERIN)
    )
    assert response.status_code == 403
    response = client.delete(group_path(service_id, TEAM_GROUP), headers=headers(ALICE))
    assert response.status_code == 200
    assert response.get_json() == TEAM_GROUP
    assert group_permissions(client, service_id) == []
    assert update(client, service_id, ERIN) == 403
    # the roles of groups are revoked with the service
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert delete(client, service_id, ERIN) == 200
    assert update(client, service_id, ADMIN) == 201
    assert group_permissions(client, service_id) == []


def test_group_membership_required(client: FlaskClient) -> None:
    """Owners cannot grant roles to groups they are not a member of.

    Admins can grant roles to any group. Any owner can revoke roles of groups.
    """
    service_id = register(client)
    assert grant_group(client, service_id, OTHER_GROUP, "maintainer") == 403
    assert grant_group(client, service_id, OTHER_GROUP, "owner") == 403
    assert group_permissions(client, service_id) == []
    assert update(client, service_id, BOB) == 403
    assert grant_group(client, service_id, OTHER_GROUP, "maintainer", ADMIN) == 200
    assert update(client, service_id, BOB) == 200
    # changing the role of a group is granting it, too
    assert grant_group(client, service_id, OTHER_GROUP, "owner") == 403
    assert revoke_group(client, service_id, OTHER_GROUP) == 200
    assert update(client, service_id, BOB) == 403
    # other owners can't grant roles to groups they aren't members of either,
    # but to their own groups
    assert grant(client, service_id, BOB, "owner") == 200
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer", BOB) == 403
    assert grant_group(client, service_id, OTHER_GROUP, "maintainer", BOB) == 200
    assert group_permissions(client, service_id) == [(OTHER_GROUP, "maintainer")]


def test_invalid_group_permissions(client: FlaskClient) -> None:
    """Invalid roles and groups, unknown services and missing roles are rejected."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "admin") == 400
    assert grant_group(client, service_id, "a,b", "owner", ADMIN) == 400
    assert grant_group(client, "UNKNWN", TEAM_GROUP, "owner", ADMIN) == 404
    assert revoke_group(client, "UNKNWN", TEAM_GROUP, as_user=ADMIN) == 404
    assert revoke_group(client, service_id, TEAM_GROUP) == 404
    assert revoke_group(client, service_id, "a,b") == 404
    assert permission_list(client, service_id) == [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"}
    ]


@pytest.mark.parametrize("path", ["groups", "groups/"])
def test_group_permissions_without_group(
    app: App, client: FlaskClient, path: str
) -> None:
    """Group roles without a group are not found.

    Nor are they taken as roles of a user `groups` (which would also count as
    another owner).
    """
    service_id = register(client)
    url = f"{BASE}/services/{service_id}/permissions/{path}"
    response = client.put(url, json={"role": "owner"}, headers=headers(ALICE))
    assert response.status_code == 404
    assert response.get_json()["status"] == 404
    assert permission_list(client, service_id) == [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"}
    ]
    assert revoke(client, service_id, ALICE) == 409
    # nor is a role of a user `groups` revoked (e.g., one stored before)
    with app.app.test_request_context(f"{BASE}/services/{service_id}"):
        add_resource_role(
            subject="groups",
            role="maintainer",
            resource=permissions_module.service_path(service_id),
        )
    assert client.delete(url, headers=headers(ALICE)).status_code == 404
    assert permissions(client, service_id) == [
        (ALICE, "owner"),
        ("groups", "maintainer"),
    ]


def test_group_with_slash(client: FlaskClient) -> None:
    """Groups may contain slashes (e.g., URLs), URL-encoded in the path."""
    service_id = register(client)
    group = "https://idp.example.org/groups/team?x=1#y"
    assert grant_group(client, service_id, group, "maintainer", ADMIN) == 200
    assert group_permissions(client, service_id) == [(group, "maintainer")]
    assert revoke_group(client, service_id, group) == 200
    assert group_permissions(client, service_id) == []


@pytest.mark.parametrize("kind", ["", "groups/"], ids=["user", "group"])
@pytest.mark.parametrize("principal", [".", "..", "%2E", "%2E%2E"])
def test_dot_segment_principals(client: FlaskClient, kind: str, principal: str) -> None:
    """Users and groups that are dot segments are rejected.

    Clients would resolve their URLs to other ones (e.g., `encodeURIComponent`
    leaves dots unencoded).
    """
    service_id = register(client)
    url = f"{BASE}/services/{service_id}/permissions/{kind}{principal}"
    response = client.put(url, json={"role": "owner"}, headers=headers(ADMIN))
    assert response.status_code == 400
    assert client.delete(url, headers=headers(ADMIN)).status_code == 400
    assert permission_list(client, service_id) == [
        {"user_id": ALICE, "name": NAMES[ALICE], "role": "owner"}
    ]


@pytest.mark.parametrize(
    "group",
    ["../a", "a/..", "a/./b", "a/../b", "./a", "a/.", "a%2F..%2Fb", "..%5Cb", "a%5C."],
)
def test_dot_segments_in_groups(client: FlaskClient, group: str) -> None:
    """Groups with dot segments between slashes are rejected.

    Clients that don't encode slashes would resolve their URLs to other ones
    (e.g., `groups/../a` to the user `a`).
    """
    service_id = register(client)
    url = f"{BASE}/services/{service_id}/permissions/groups/{group}"
    response = client.put(url, json={"role": "owner"}, headers=headers(ADMIN))
    assert response.status_code == 400
    assert client.delete(url, headers=headers(ADMIN)).status_code == 400
    assert group_permissions(client, service_id) == []


@pytest.mark.parametrize("group", ["a/...", "a/.b/c", "a./b", "https://a.org/g"])
def test_dotted_groups(client: FlaskClient, group: str) -> None:
    """Groups with dots in segments that aren't dot segments are accepted."""
    service_id = register(client)
    assert grant_group(client, service_id, group, "maintainer", ADMIN) == 200
    assert group_permissions(client, service_id) == [(group, "maintainer")]


@pytest.mark.parametrize("user", ["..%5Cb", "a%5C.."])
def test_dot_segments_in_users(client: FlaskClient, user: str) -> None:
    """Users with dot segments between backslashes are rejected.

    Browsers take backslashes in URLs as slashes.
    """
    service_id = register(client)
    url = f"{BASE}/services/{service_id}/permissions/{user}"
    response = client.put(url, json={"role": "owner"}, headers=headers(ADMIN))
    assert response.status_code == 400
    assert client.delete(url, headers=headers(ADMIN)).status_code == 400


@pytest.mark.parametrize("principal", ["...", ".a", "a."])
def test_dotted_principals(client: FlaskClient, principal: str) -> None:
    """Users and groups with dots that aren't dot segments are accepted."""
    service_id = register(client)
    assert grant(client, service_id, principal, "maintainer", ADMIN) == 200
    assert grant_group(client, service_id, principal, "maintainer", ADMIN) == 200
    assert revoke(client, service_id, principal, as_user=ADMIN) == 200
    assert revoke_group(client, service_id, principal, as_user=ADMIN) == 200


def test_users_and_groups_distinct(client: FlaskClient) -> None:
    """A user whose identifier starts with `group:` is not a group."""
    service_id = register(client)
    user = quote(f"group:{TEAM_GROUP}", safe="")
    assert grant(client, service_id, user, "owner") == 200
    assert update(client, service_id, ERIN) == 403
    assert group_permissions(client, service_id) == []
    assert permissions(client, service_id) == [
        (ALICE, "owner"),
        (f"group:{TEAM_GROUP}", "owner"),
    ]
    assert revoke_group(client, service_id, TEAM_GROUP) == 404


def test_group_last_owner(client: FlaskClient) -> None:
    """Groups count as owners.

    The last user owner can be removed while a group is owner, and the last
    owner group can't be removed or downgraded, not even by admins.
    """
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert revoke(client, service_id, ALICE, as_user=ERIN) == 200
    assert permission_list(client, service_id) == [
        {"group": TEAM_GROUP, "role": "owner"}
    ]
    # alice is still an owner, as a member of the group
    assert update(client, service_id, ALICE) == 200
    assert revoke_group(client, service_id, TEAM_GROUP, as_user=ERIN) == 409
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer", ERIN) == 409
    assert revoke_group(client, service_id, TEAM_GROUP, as_user=ADMIN) == 409
    assert grant_group(client, service_id, TEAM_GROUP, "owner", ERIN) == 200
    # with another owner, the group can be removed
    assert grant(client, service_id, ERIN, "owner", as_user=ERIN) == 200
    assert revoke_group(client, service_id, TEAM_GROUP, as_user=ERIN) == 200
    assert revoke(client, service_id, ERIN, as_user=ERIN) == 409
    assert permission_list(client, service_id) == [{"user_id": ERIN, "role": "owner"}]


@pytest.mark.parametrize("change", ["revoke", "downgrade"])
def test_last_owner_group_restored(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """If concurrent changes removed all owners, the owner group is restored."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert revoke(client, service_id, ALICE) == 200
    monkeypatch.setattr(
        ownership_module, "_check_other_owners", lambda *args, **kwargs: None
    )
    if change == "revoke":
        assert revoke_group(client, service_id, TEAM_GROUP, as_user=ERIN) == 409
    else:
        assert grant_group(client, service_id, TEAM_GROUP, "maintainer", ERIN) == 409
    assert group_permissions(client, service_id) == [(TEAM_GROUP, "owner")]


def test_group_grant_racing_delete(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A group's role on a service deleted meanwhile is revoked again (`404`)."""
    service_id = register(client)
    with monkeypatch.context() as patch:
        deleted = delete_after_first_check(patch, service_id)
        assert grant_group(client, service_id, TEAM_GROUP, "maintainer") == 404
    assert deleted == [True]
    assert _roles(app, service_id) == [(ALICE, "owner")]


def test_me_group_roles(client: FlaskClient) -> None:
    """`GET /me` also lists the roles users have through groups."""
    first, second = register(client), register(client)
    assert grant_group(client, first, TEAM_GROUP, "maintainer") == 200
    assert grant_group(client, second, OTHER_GROUP, "owner", ADMIN) == 200
    assert me(client, ERIN)["services"] == [
        {"service_id": first, "role": "maintainer", "group": TEAM_GROUP}
    ]
    assert me(client, BOB)["services"] == [
        {"service_id": second, "role": "owner", "group": OTHER_GROUP}
    ]
    # sorted by service identifier, roles of the user first
    expected = [
        {"service_id": first, "role": "owner"},
        {"service_id": first, "role": "maintainer", "group": TEAM_GROUP},
    ]
    if second < first:
        expected.insert(0, {"service_id": second, "role": "owner"})
    else:
        expected.append({"service_id": second, "role": "owner"})
    assert me(client, ALICE)["services"] == expected
    # names are only stored for roles granted to users
    assert _names(client, first) == {ALICE: NAMES[ALICE]}


def test_group_role_changes_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Changes of roles of groups are logged like those of users."""
    caplog.set_level("INFO")
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer") == 200
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert revoke_group(client, service_id, TEAM_GROUP) == 200
    resource = f"{BASE}/services/{service_id}"
    assert _role_messages(caplog)[1:] == [
        f"Role 'maintainer' on '{resource}' granted to group '{TEAM_GROUP}' by "
        f"user '{ALICE}' (previous roles: none)",
        f"Role 'owner' on '{resource}' granted to group '{TEAM_GROUP}' by user "
        f"'{ALICE}' (previous roles: maintainer)",
        f"Roles on '{resource}' of group '{TEAM_GROUP}' revoked by user "
        f"'{ALICE}' (previous roles: owner)",
    ]


# Read operations, which are public by default (`security: []`)
READS = [
    "/services",
    "/services/types",
    "/services/{id}",
    "/services/{id}/verification",
    "/verifications",
]
DOCS = Path(__file__).parents[1] / "docs" / "access-control.md"


def _verified_service(client: FlaskClient) -> str:
    """Register and verify a service.

    Args:
        client: Test client of the app.

    Returns:
        Identifier of the service.
    """
    service_id = register(client)
    response = client.put(
        f"{BASE}/services/{service_id}/verification", headers=headers(ADMIN)
    )
    assert response.status_code == 200, response.get_json()
    return service_id


def _reads(client: FlaskClient, service_id: str, user: str | None) -> list[int]:
    """Read a service and the lists, as a user.

    Args:
        client: Test client of the app.
        service_id: Identifier of a verified service.
        user: User who reads; no token if `None`.

    Returns:
        Status codes of the read operations (`READS`), in order.
    """
    return [
        client.get(BASE + path.format(id=service_id), headers=headers(user)).status_code
        for path in READS
    ]


def test_reads_public(client: FlaskClient) -> None:
    """Reads are public by default, with and without token."""
    service_id = _verified_service(client)
    assert _reads(client, service_id, None) == [200] * len(READS)
    assert _reads(client, service_id, BOB) == [200] * len(READS)


def _docs_overlay(title: str) -> dict[str, Any]:
    """Get an overlay of the recipe "Requiring login for reads" of the docs.

    Args:
        title: Title of the overlay's YAML block (`title="<title>"`).

    Returns:
        The overlay.
    """
    section = DOCS.read_text().split("## Requiring login for reads\n", 1)[1]
    section = section.split("\n## ", 1)[0]
    block = re.search(
        rf'```yaml title="{re.escape(title)}"\n(.*?)```', section, re.DOTALL
    )
    assert block, f"The recipe has no overlay '{title}'"
    overlay: dict[str, Any] = yaml.safe_load(block[1])
    return overlay


def _create_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *overlays: dict[str, Any]
) -> FlaskClient:
    """Create the app with config overlays on top of the test overlay.

    Args:
        monkeypatch: Fixture to set the environment.
        tmp_path: Directory to write the overlays to.
        overlays: Config overlays, in order.

    Returns:
        Test client of the app.
    """
    paths = [str(CONFIG_OVERLAY)]
    for index, overlay in enumerate(overlays):
        path = tmp_path / f"overlay-{index}.yaml"
        path.write_text(yaml.safe_dump(overlay))
        paths.append(str(path))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", os.pathsep.join(paths))
    return cloud_registry.app.create_app().app.test_client()


def test_login_for_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The recipe of the docs requires login for all reads but service info.

    Anonymous reads are rejected (`401`), any authenticated user may read (by
    the default policies), and `GET /service-info`, the specification and the
    Swagger UI stay public. FOCA logs the patch, which makes no operation
    public.
    """
    client = _create_app(monkeypatch, tmp_path, _docs_overlay("overlay.yaml"))
    err = capsys.readouterr().err
    assert "Applied patches to API spec" in err
    assert "api/patches/login-for-reads.yaml" in err
    assert "not require authentication" not in err
    service_id = _verified_service(client)
    assert _reads(client, service_id, None) == [401] * len(READS)
    assert _reads(client, service_id, BOB) == [200] * len(READS)
    assert _reads(client, service_id, ADMIN) == [200] * len(READS)
    for path in ["/service-info", "/openapi.json", "/ui/"]:
        assert client.get(BASE + path).status_code == 200, path
    # other operations keep their protections
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(BOB)
    )
    assert response.status_code == 403
    assert update(client, service_id, BOB) == 403


def test_login_for_reads_readers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """With the rules for readers of the docs, only readers may read.

    Readers (here: members of the team group) may list and get services and
    get the types, not read verifications; other users may read nothing, also
    not services they own; admins may read everything.
    """
    readers = _docs_overlay("readers.yaml")
    mapping = readers["security"]["access_control"]["group_mapping"]
    # only readers are mapped, the admin group of the deployment is kept
    assert list(mapping.values()) == ["reader"]
    # the team group of the test identity provider instead of the example's
    readers["security"]["access_control"]["group_mapping"] = {TEAM_GROUP: "reader"}
    client = _create_app(monkeypatch, tmp_path, _docs_overlay("overlay.yaml"), readers)
    service_id = _verified_service(client)
    # in the order of `READS`: services, types, service, its verification,
    # verifications
    assert _reads(client, service_id, ALICE) == [200, 200, 200, 403, 403]
    assert _reads(client, service_id, BOB) == [403] * len(READS)
    assert _reads(client, service_id, ADMIN) == [200] * len(READS)


def _duplicates(
    client: FlaskClient, existing_id: str, user: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Register a service, and change one, to the URL and type of another one.

    Args:
        client: Test client of the app.
        existing_id: Identifier of the registered service.
        user: User who registers (and owns) the services.

    Returns:
        Problem details of the `409` responses: of registering the service
        again (`POST`) and of changing another service of the user to it
        (`PUT`).
    """
    existing = client.get(f"{BASE}/services/{existing_id}", headers=headers(ADMIN))
    assert existing.status_code == 200, existing.get_json()
    service = {
        key: value
        for key, value in existing.get_json().items()
        if key not in ("id", "createdAt", "updatedAt")
    }
    responses = [
        client.post(f"{BASE}/services", json=service, headers=headers(user)),
        client.put(
            f"{BASE}/services/{register(client, user)}",
            json=service,
            headers=headers(user),
        ),
    ]
    for response in responses:
        assert response.status_code == 409, response.get_json()
    return responses[0].get_json(), responses[1].get_json()


def _assert_named(problem: dict[str, Any], existing_id: str) -> None:
    """Assert that the problem details of a `409` name the registered service.

    Args:
        problem: Problem details.
        existing_id: Identifier of the registered service.
    """
    assert problem["serviceId"] == existing_id
    assert existing_id in problem["detail"]


def _assert_hidden(problem: dict[str, Any], existing_id: str) -> None:
    """Assert that the problem details of a `409` hide the registered service.

    Args:
        problem: Problem details.
        existing_id: Identifier of the registered service.
    """
    assert problem == {
        "title": "Conflict",
        "status": 409,
        "detail": DuplicateService.HIDDEN_DESCRIPTION,
    }
    assert existing_id not in str(problem)


def test_duplicates_public_reads(client: FlaskClient) -> None:
    """With public reads, `409` names the registered service to every user."""
    existing_id = register(client)
    for user in (ALICE, BOB, ADMIN):
        for problem in _duplicates(client, existing_id, user):
            _assert_named(problem, existing_id)


def test_duplicates_login_for_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """With login for reads by any user, `409` names the registered service."""
    client = _create_app(monkeypatch, tmp_path, _docs_overlay("overlay.yaml"))
    existing_id = register(client)
    for user in (ALICE, BOB, ADMIN):
        for problem in _duplicates(client, existing_id, user):
            _assert_named(problem, existing_id)


def test_duplicates_login_for_reads_readers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """With the rules for readers, `409` names the service to readers only.

    Readers (members of the team group) and admins learn the registered
    service, other users get a `409` without it, also for services they
    registered themselves.
    """
    readers = _docs_overlay("readers.yaml")
    readers["security"]["access_control"]["group_mapping"] = {TEAM_GROUP: "reader"}
    client = _create_app(monkeypatch, tmp_path, _docs_overlay("overlay.yaml"), readers)
    for owner in (ALICE, BOB):
        existing_id = register(client, owner)
        for user in (ALICE, ADMIN):
            for problem in _duplicates(client, existing_id, user):
                _assert_named(problem, existing_id)
        for problem in _duplicates(client, existing_id, BOB):
            _assert_hidden(problem, existing_id)


@pytest.mark.parametrize("service_id", ["a/b", "a/verification", "a/permissions"])
def test_may_read_service_unrouted(client: FlaskClient, service_id: str) -> None:
    """Identifiers not routed to `GET /services/{serviceId}` can't be read.

    Also not those routed to other operations, which policies may allow.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
    """
    with client.application.test_request_context(f"{BASE}/services", method="POST"):
        assert not may_read_service(service_id)
