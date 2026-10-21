"""Tests for access control, with the app created from its config (cf.
`tests/conftest.py`).
"""

from copy import deepcopy
from itertools import count

import mongomock
import pytest

from cloud_registry.ga4gh.registry import permissions as permissions_module
from cloud_registry.ga4gh.registry import server as server_module
from cloud_registry.utils import get_collection
from tests.conftest import ADMIN, NAMES, USER, make_headers

BASE = "/ga4gh/registry/v1"
SERVICE = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
# Users: alice (owner of services she registers), bob (member of another
# group), carol (no name in user info), dave (blank name), and an admin
ALICE, BOB, CAROL = USER, "bob@example.org", "carol@example.org"
DAVE = "dave@example.org"


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


def permissions(client, service_id, user=ADMIN):
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(user)
    )
    assert response.status_code == 200, response.json
    return sorted((p["user_id"], p["role"]) for p in response.json)


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
    return {p["user_id"]: p.get("name") for p in response.json}


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
    replace_one = mongomock.collection.Collection.replace_one
    deleted = []

    def delete_first(self, *args, **kwargs):
        # the owner deletes the service after it was read for the update
        if self.name == "services" and not deleted:
            deleted.append(True)
            permissions_module.remove_permissions(service_id)
            self.delete_one({"id": service_id})
        return replace_one(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mongomock.collection.Collection, "replace_one", delete_first)
        assert update(client, service_id, user) == status
    assert deleted
    assert _roles(app, service_id) == roles
    expected = 404 if status == 404 else 200
    assert client.get(f"{BASE}/services/{service_id}").status_code == expected


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

    def delete_first(**kwargs):
        # the service is deleted after its existence was checked
        permissions_module.remove_permissions(service_id)
        get_collection("services").delete_one({"id": service_id})
        return add_resource_role(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(permissions_module, "add_resource_role", delete_first)
        assert grant(client, service_id, BOB, "maintainer") == 404
    assert _roles(app, service_id) == []


def test_delete_racing_grant(app, client, monkeypatch):
    """Roles granted while a service is deleted (after its roles were
    revoked) are revoked with it.
    """
    service_id = register(client)
    delete_one = mongomock.collection.Collection.delete_one

    def grant_first(self, *args, **kwargs):
        # a role is granted after the roles were revoked, and the grant's
        # check found the service still registered
        if self.name == "services":
            permissions_module.add_resource_role(
                subject=BOB,
                role="owner",
                resource=permissions_module.service_path(service_id),
            )
        return delete_one(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(mongomock.collection.Collection, "delete_one", grant_first)
        assert delete(client, service_id, ALICE) == 200
    assert _roles(app, service_id) == []
