"""Tests for access control, with the app created from its config (cf.
`tests/conftest.py`).
"""

from copy import deepcopy

import pytest

from cloud_registry.ga4gh.registry import permissions as permissions_module
from cloud_registry.ga4gh.registry import server as server_module
from cloud_registry.utils import get_collection
from tests.conftest import ADMIN, NAMES, USER, make_headers
from tests.mock_data import SERVICE_INFO_CONFIG

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


def register(client, user=ALICE):
    """Register a service as a user; return its identifier."""
    response = client.post(f"{BASE}/services", json=_service(), headers=headers(user))
    assert response.status_code == 200, response.json
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
    response = client.post(f"{BASE}/service-info", json=SERVICE_INFO_CONFIG)
    assert response.status_code == 401
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


def test_service_info_admin_only(client):
    """Only admins can set the service info."""
    for user, status in [
        (ALICE, 403),
        (BOB, 403),
        (ADMIN, 201),
    ]:
        response = client.post(
            f"{BASE}/service-info", json=SERVICE_INFO_CONFIG, headers=headers(user)
        )
        assert response.status_code == status, user


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
    assert update(client, "MYSERV", ADMIN) == 200
    assert permissions(client, "MYSERV") == [(ADMIN, "owner")]


def test_permissions_removed_with_service(client):
    """Roles on a service are revoked when it is deleted."""
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    assert delete(client, service_id, ALICE) == 200
    # a new service with the same identifier has none of the old roles
    assert update(client, service_id, ADMIN) == 200
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
    assert update(client, service_id, ADMIN) == 200
    assert permissions(client, service_id) == [(ADMIN, "owner")]


def test_invalid_identifier_rolled_back(client):
    """Services whose owner can't be stored are not registered."""
    response = client.put(
        f"{BASE}/services/a,b", json=_service(), headers=headers(ADMIN)
    )
    assert response.status_code == 400
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
