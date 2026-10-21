"""Tests for access control, with the app created from its config (cf.
`tests/conftest.py`).
"""

from copy import deepcopy
from itertools import count
from urllib.parse import quote

import mongomock
import pytest

from cloud_registry.ga4gh.registry import permissions as permissions_module
from cloud_registry.ga4gh.registry import server as server_module
from cloud_registry.utils import get_collection
from tests.conftest import ADMIN, ENTITLEMENTS, NAMES, TEAM_GROUP, USER, make_headers

BASE = "/ga4gh/registry/v1"
SERVICE = {
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
OTHER_GROUP = ENTITLEMENTS[BOB][0]


def headers(user=None):
    """Request headers with an access token for a user; none if `None`."""
    return {} if user is None else make_headers(user)


# Numbers for the URLs of registered services, which must be unique
URLS = count()


def register(client, user=ALICE):
    """Register a service (with a new URL) as a user; return its identifier."""
    service = {**_service(), "url": f"https://tes{next(URLS)}.example.org"}
    response = client.post(f"{BASE}/services", json=service, headers=headers(user))
    assert response.status_code == 201, response.json
    return response.json


def _service():
    return deepcopy(SERVICE)


def permission_list(client, service_id, user=ADMIN):
    """Roles granted on a service, as listed (users and groups)."""
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(user)
    )
    assert response.status_code == 200, response.json
    return response.json


def permissions(client, service_id, user=ADMIN):
    """Roles granted to users on a service."""
    return sorted(
        (p["user_id"], p["role"])
        for p in permission_list(client, service_id, user)
        if "user_id" in p
    )


def group_permissions(client, service_id, user=ADMIN):
    """Roles granted to groups on a service."""
    return sorted(
        (p["group"], p["role"])
        for p in permission_list(client, service_id, user)
        if "group" in p
    )


def grant(client, service_id, user, role, as_user=ALICE):
    return client.put(
        f"{BASE}/services/{service_id}/permissions/{user}",
        json={"role": role},
        headers=headers(as_user),
    ).status_code


def update(client, service_id, user):
    return client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=headers(user)
    ).status_code


def delete(client, service_id, user):
    return client.delete(
        f"{BASE}/services/{service_id}", headers=headers(user)
    ).status_code


def test_public_read(client):
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


def test_write_requires_authentication(client):
    """Writing requires a token."""
    service_id = register(client)
    assert client.post(f"{BASE}/services", json=_service()).status_code == 401
    assert client.put(f"{BASE}/services/X", json=_service()).status_code == 401
    assert client.delete(f"{BASE}/services/{service_id}").status_code == 401
    response = client.get(f"{BASE}/services/{service_id}/permissions")
    assert response.status_code == 401
    assert client.get(f"{BASE}/me").status_code == 401


def test_creator_is_owner(client):
    """Any authenticated user can register a service and becomes its owner."""
    service_id = register(client, user=BOB)
    assert permissions(client, service_id) == [(BOB, "owner")]
    assert update(client, service_id, BOB) == 200
    assert delete(client, service_id, BOB) == 200


def test_others_forbidden(client):
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


def test_admin(client):
    """Admins (via entitlement from user info) can modify anything."""
    service_id = register(client)
    assert update(client, service_id, ADMIN) == 200
    assert grant(client, service_id, CAROL, "owner", ADMIN) == 200
    assert delete(client, service_id, ADMIN) == 200


def test_maintainer(client):
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


def test_owners_manage_permissions(client):
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


def test_invalid_permissions(client):
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


def test_put_create(client):
    """Only admins can register services with chosen identifiers."""
    assert update(client, "MYSERV", ALICE) == 403
    assert update(client, "MYSERV", ADMIN) == 201
    assert permissions(client, "MYSERV") == [(ADMIN, "owner")]


def test_permissions_removed_with_service(client):
    """Roles on a service are revoked when it is deleted."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert delete(client, service_id, ALICE) == 200
    # a new service with the same identifier has none of the old roles
    assert update(client, service_id, ADMIN) == 201
    assert permissions(client, service_id) == [(ADMIN, "owner")]
    assert update(client, service_id, BOB) == 403


def revoke(client, service_id, user, as_user=ALICE):
    return client.delete(
        f"{BASE}/services/{service_id}/permissions/{user}", headers=headers(as_user)
    ).status_code


def me(client, user):
    response = client.get(f"{BASE}/me", headers=headers(user))
    assert response.status_code == 200, response.json
    return response.json


def test_me(client):
    """Users get their identifier, admin status and roles on services."""
    first, second = register(client), register(client)
    assert grant(client, first, BOB, "maintainer") == 200
    assert me(client, ALICE) == {
        "user_id": ALICE,
        "admin": False,
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


def _names(client, service_id):
    response = client.get(
        f"{BASE}/services/{service_id}/permissions",
        headers=headers(ADMIN),
    )
    return {p["user_id"]: p.get("name") for p in response.json if "user_id" in p}


def test_permission_names(client):
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


def test_last_owner(client):
    """The last owner of a service cannot be removed or downgraded, not even
    by admins.
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


def test_names_per_service(client):
    """Names are only shown for users who used the registry with their role
    on the service, and are removed with the role.
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
def test_last_owner_restored(client, monkeypatch, change):
    """If concurrent changes removed all owners, the removed owner is
    restored.
    """
    service_id = register(client)
    # skip the check before the change, so that only the check afterwards can
    # keep the service from losing its last owner (as when concurrent
    # requests removed the other owners after the first check)
    monkeypatch.setattr(permissions_module, "_check_other_owners", lambda *args: None)
    if change == "revoke":
        assert revoke(client, service_id, ALICE) == 409
    else:
        assert grant(client, service_id, ALICE, "maintainer") == 409
    assert permissions(client, service_id) == [(ALICE, "owner")]


def test_stale_roles_cleared(client, monkeypatch):
    """Roles left on the identifier of a deleted service don't apply to a new
    service with that identifier.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "owner") == 200
    # as if revoking the roles of the deleted service had failed
    with monkeypatch.context() as patch:
        patch.setattr(server_module, "remove_permissions", lambda serviceId: None)
        assert delete(client, service_id, ALICE) == 200
    assert update(client, service_id, ADMIN) == 201
    assert permissions(client, service_id) == [(ADMIN, "owner")]


def test_invalid_identifier_rolled_back(client):
    """Services whose owner can't be stored are not registered."""
    response = client.put(
        f"{BASE}/services/a,b", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 400
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services/a,b").status_code == 404


def test_stale_name_not_shown(app, client):
    """A name stored before a role is granted is not shown with the role."""
    service_id = register(client)
    with app.app.app_context():
        get_collection("user_names").insert_one(
            {"service_id": service_id, "user_id": BOB, "name": "Stale"}
        )
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert _names(client, service_id)[BOB] is None


def test_owner_failure_rolled_back(client, monkeypatch):
    """Services whose owner can't be stored for other reasons are not
    registered either.
    """

    def fail(serviceId):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(server_module, "add_owner", fail)
    response = client.put(
        f"{BASE}/services/MYSERV", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 500
    assert client.get(f"{BASE}/services/MYSERV").status_code == 404


def test_failed_grant_reduces_rights(client, monkeypatch):
    """If granting a role fails, the user's other roles are revoked already."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "owner") == 200

    def fail(**kwargs):
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(permissions_module, "add_resource_role", fail)
        assert grant(client, service_id, BOB, "maintainer") == 500
    assert permissions(client, service_id) == [(ALICE, "owner")]


def test_permissions_of_unknown_service(client):
    """Permissions of unknown services are not found."""
    response = client.get(f"{BASE}/services/UNKNWN/permissions", headers=headers(ADMIN))
    assert response.status_code == 404


def test_revoke_invalid_user_id(client):
    """Roles of invalid user identifiers are not found."""
    service_id = register(client)
    assert revoke(client, service_id, "bob,x") == 404


def test_owner_not_restored_on_deleted_service(client, monkeypatch):
    """No owner is restored for a service deleted concurrently."""
    service_id = register(client)
    calls = []

    def exists_once(serviceId):
        # The service exists for the first check only.
        calls.append(serviceId)
        return len(calls) == 1

    with monkeypatch.context() as patch:
        patch.setattr(permissions_module, "_check_other_owners", lambda *args: None)
        patch.setattr(permissions_module, "service_exists", exists_once)
        assert revoke(client, service_id, ALICE) == 404
    assert permissions(client, service_id) == []


def test_no_claims_without_token(app):
    """Without a validated token, there are no claims."""
    with app.app.test_request_context():
        assert permissions_module._get_current_claims() == {}


def _roles(app, service_id):
    """Roles granted on a service, also if it doesn't exist."""
    with app.app.test_request_context(f"{BASE}/services/{service_id}"):
        app.app.preprocess_request()
        return sorted(
            permissions_module.list_resource_roles(f"{BASE}/services/{service_id}")
        )


def before_first_call(patch, method, collection, callback):
    """Run a callback once, before the first call of a method of `mongomock`
    collections on a collection (e.g., to interleave another request).

    Args:
        patch: Monkeypatch (context) to patch the method with.
        method: Name of the method of `mongomock.collection.Collection`.
        collection: Name of the collection.
        callback: Function to call, without arguments.

    Returns:
        List that is non-empty once the callback ran.
    """
    original = getattr(mongomock.collection.Collection, method)
    fired = []

    def wrapper(self, *args, **kwargs):
        if self.name == collection and not fired:
            fired.append(True)
            callback()
        return original(self, *args, **kwargs)

    patch.setattr(mongomock.collection.Collection, method, wrapper)
    return fired


@pytest.mark.parametrize(
    ("user", "status", "roles"),
    [(BOB, 404, []), (ADMIN, 201, [(ADMIN, "owner")])],
    ids=["maintainer", "admin"],
)
def test_put_racing_delete(app, client, monkeypatch, user, status, roles):
    """A service deleted while it is replaced is only registered again by
    admins: others get `404`, and don't become owners.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200

    def delete_service():
        # the owner deletes the service after it was read for the update
        permissions_module.remove_permissions(service_id)
        get_collection("services").delete_one({"id": service_id})

    with monkeypatch.context() as patch:
        fired = before_first_call(patch, "replace_one", "services", delete_service)
        assert update(client, service_id, user) == status
    assert fired
    assert _roles(app, service_id) == roles
    expected = 404 if status == 404 else 200
    assert client.get(f"{BASE}/services/{service_id}").status_code == expected


def test_create_racing_delete(app, client, monkeypatch, caplog):
    """The owner role (and name) of the creator of a new service deleted
    while its owner is stored is revoked again (`404`); roles granted by
    others on the identifier (e.g., to the owner of a service registered
    again with it) are kept.
    """

    def delete_service():
        # the service is deleted before its owner role is checked (its roles
        # were revoked before the owner role was granted); and another
        # request grants a role on the identifier
        get_collection("services").delete_one({"id": "MYSERV"})
        permissions_module.add_resource_role(
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


def test_put_stale_role(app, client, monkeypatch):
    """Roles left on the identifier of a deleted service don't let users
    other than admins register it again.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    # as if revoking the roles of the deleted service had failed
    with monkeypatch.context() as patch:
        patch.setattr(server_module, "remove_permissions", lambda serviceId: None)
        assert delete(client, service_id, ALICE) == 200
    assert update(client, service_id, BOB) == 404
    assert client.get(f"{BASE}/services/{service_id}").status_code == 404


def test_roles_removed_before_delete(app, client, monkeypatch):
    """Roles on a service are revoked before it is deleted, so that no role
    is left on a deleted service if deleting it fails.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200

    def fail(self, *args, **kwargs):
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(mongomock.collection.Collection, "delete_one", fail)
        assert delete(client, service_id, ALICE) == 500
    assert _roles(app, service_id) == []
    assert client.get(f"{BASE}/services/{service_id}").status_code == 200


def test_grant_racing_delete(app, client, monkeypatch):
    """A role granted on a service that is deleted meanwhile is revoked
    again (`404`).
    """
    service_id = register(client)
    add_resource_role = permissions_module.add_resource_role
    deleted = []

    def delete_first(**kwargs):
        # the service is deleted after its existence was checked
        deleted.append(True)
        permissions_module.remove_permissions(service_id)
        get_collection("services").delete_one({"id": service_id})
        return add_resource_role(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(permissions_module, "add_resource_role", delete_first)
        assert grant(client, service_id, BOB, "maintainer") == 404
    assert deleted == [True]
    assert _roles(app, service_id) == []


def test_delete_racing_grant(app, client, monkeypatch):
    """Roles granted while a service is deleted (after its roles were
    revoked) are revoked with it.
    """
    service_id = register(client)

    def grant_role():
        # a role is granted after the roles were revoked, and the grant's
        # check found the service still registered
        permissions_module.add_resource_role(
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


def test_permissions_api_admin_only(client):
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
    assert response.json["pagination"]["total"] > 0


def test_admin_via_permissions_api(client):
    """Users granted the role `admin` via the permissions API are admins,
    and `GET /me` says so, until the role is revoked.
    """
    service_id = register(client)
    assert me(client, CAROL)["admin"] is False
    rule = {"policy_type": "g", "rule": {"v0": f"user:{CAROL}", "v1": "admin"}}
    response = client.post(PERMISSIONS_API, json=rule, headers=headers(ADMIN))
    assert response.status_code == 200
    rule_id = response.json
    assert me(client, CAROL)["admin"] is True
    assert update(client, service_id, CAROL) == 200
    response = client.delete(f"{PERMISSIONS_API}/{rule_id}", headers=headers(ADMIN))
    assert response.status_code == 200
    assert me(client, CAROL)["admin"] is False
    assert update(client, service_id, CAROL) == 403


def test_role_changes_logged(client, caplog):
    """Changes of roles are logged: who changed which role of whom on which
    service.
    """
    caplog.set_level("INFO", logger=permissions_module.__name__)
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert grant(client, service_id, BOB, "owner") == 200
    assert revoke(client, service_id, BOB) == 200
    assert delete(client, service_id, ADMIN) == 200
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == permissions_module.__name__
    ]
    assert messages == [
        f"Role 'owner' on new service '{service_id}' granted to its creator '{ALICE}'.",
        f"Role 'maintainer' on service '{service_id}' granted to '{BOB}' by "
        f"'{ALICE}' (previous roles: none).",
        f"Role 'owner' on service '{service_id}' granted to '{BOB}' by "
        f"'{ALICE}' (previous roles: maintainer).",
        f"Roles on service '{service_id}' of '{BOB}' revoked by '{ALICE}'.",
        f"All roles on service '{service_id}' revoked by '{ADMIN}'.",
    ]
    assert f"Service '{service_id}' deleted by '{ADMIN}'." in caplog.text


def group_path(service_id, group):
    """Path of the roles of a group on a service (URL-encoded group)."""
    return f"{BASE}/services/{service_id}/permissions/groups/{quote(group, safe='')}"


def grant_group(client, service_id, group, role, as_user=ALICE):
    return client.put(
        group_path(service_id, group), json={"role": role}, headers=headers(as_user)
    ).status_code


def revoke_group(client, service_id, group, as_user=ALICE):
    return client.delete(
        group_path(service_id, group), headers=headers(as_user)
    ).status_code


def test_group_roles(client):
    """Owners can grant roles to groups they are a member of; all members of
    the group then have the role.
    """
    service_id = register(client)
    response = client.put(
        group_path(service_id, TEAM_GROUP),
        json={"role": "owner"},
        headers=headers(ALICE),
    )
    assert response.status_code == 200
    assert response.json == {"group": TEAM_GROUP, "role": "owner"}
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
    assert response.json == TEAM_GROUP
    assert group_permissions(client, service_id) == []
    assert update(client, service_id, ERIN) == 403
    # the roles of groups are revoked with the service
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert delete(client, service_id, ERIN) == 200
    assert update(client, service_id, ADMIN) == 201
    assert group_permissions(client, service_id) == []


def test_group_membership_required(client):
    """Owners cannot grant roles to groups they are not a member of; admins
    can grant roles to any group. Any owner can revoke roles of groups.
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
    # members of a group can't grant themselves roles through it
    assert grant_group(client, service_id, TEAM_GROUP, "owner", as_user=ERIN) == 403


def test_invalid_group_permissions(client):
    """Invalid roles and groups, unknown services and missing roles are
    rejected.
    """
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


def test_group_with_slash(client):
    """Groups may contain slashes (e.g., URLs), URL-encoded in the path."""
    service_id = register(client)
    group = "https://idp.example.org/groups/team?x=1#y"
    assert grant_group(client, service_id, group, "maintainer", ADMIN) == 200
    assert group_permissions(client, service_id) == [(group, "maintainer")]
    assert revoke_group(client, service_id, group) == 200
    assert group_permissions(client, service_id) == []


def test_users_and_groups_distinct(client):
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


def test_group_last_owner(client):
    """Groups count as owners: the last user owner can be removed while a
    group is owner, and the last owner group can't be removed or
    downgraded, not even by admins.
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
def test_last_owner_group_restored(client, monkeypatch, change):
    """If concurrent changes removed all owners, the removed owner group is
    restored.
    """
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert revoke(client, service_id, ALICE) == 200
    monkeypatch.setattr(
        permissions_module, "_check_other_owners", lambda *args, **kwargs: None
    )
    if change == "revoke":
        assert revoke_group(client, service_id, TEAM_GROUP, as_user=ERIN) == 409
    else:
        assert grant_group(client, service_id, TEAM_GROUP, "maintainer", ERIN) == 409
    assert group_permissions(client, service_id) == [(TEAM_GROUP, "owner")]


def test_group_grant_racing_delete(app, client, monkeypatch):
    """A role granted to a group on a service that is deleted meanwhile is
    revoked again (`404`).
    """
    service_id = register(client)
    add_resource_role = permissions_module.add_resource_role

    def delete_first(**kwargs):
        # the service is deleted after its existence was checked
        permissions_module.remove_permissions(service_id)
        get_collection("services").delete_one({"id": service_id})
        return add_resource_role(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(permissions_module, "add_resource_role", delete_first)
        assert grant_group(client, service_id, TEAM_GROUP, "maintainer") == 404
    assert _roles(app, service_id) == []


def test_me_group_roles(client):
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


def test_group_role_changes_logged(client, caplog):
    """Changes of roles of groups are logged like those of users."""
    caplog.set_level("INFO", logger=permissions_module.__name__)
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "maintainer") == 200
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert revoke_group(client, service_id, TEAM_GROUP) == 200
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == permissions_module.__name__
    ]
    assert messages[1:] == [
        f"Role 'maintainer' on service '{service_id}' granted to group "
        f"'{TEAM_GROUP}' by '{ALICE}' (previous roles: none).",
        f"Role 'owner' on service '{service_id}' granted to group "
        f"'{TEAM_GROUP}' by '{ALICE}' (previous roles: maintainer).",
        f"Roles on service '{service_id}' of group '{TEAM_GROUP}' revoked by "
        f"'{ALICE}'.",
    ]
