"""Tests for invitations to services, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`; the helpers for
services and roles are those of `tests/test_access_control.py` and
`tests/test_rename.py`.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import os
from typing import Any
from urllib.parse import quote

from connexion import App
from flask.testing import FlaskClient
from foca import get_custom_config
from foca.security import auth
from foca.security.access_control import list_roles, remove_resource_role
import pytest
from werkzeug.test import TestResponse

from cloud_registry.ga4gh.registry import invitations as invitations_module
from cloud_registry.ga4gh.registry import locks as locks_module
from cloud_registry.ga4gh.registry.invitations import (
    TOKEN_BYTES,
    hash_token,
)
from cloud_registry.ga4gh.registry.locks import LOCKS
from cloud_registry.ga4gh.registry.permissions import (
    INVITATIONS,
    USER_NAMES,
    is_manageable_user_id,
)
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection, now
from tests.conftest import ADMIN, GROUPS, NAMES, TEAM_GROUP
from tests.test_access_control import (
    ALICE,
    BASE,
    BOB,
    CAROL,
    ERIN,
    delete,
    grant,
    grant_group,
    headers,
    permission_list,
    permissions,
    register,
    revoke,
    update,
)
from tests.test_rename import NEW_ID, PATH, make_lock, move_service, rename, resume


def invite(
    client: FlaskClient, service_id: str, role: str = "maintainer", user: str = ALICE
) -> TestResponse:
    """Create an invitation to a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        role: Role of the invitation.
        user: User who creates the invitation.

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/services/{service_id}/invitations",
        json={"role": role},
        headers=headers(user),
    )


def token_of(client: FlaskClient, service_id: str, role: str = "maintainer") -> str:
    """Create an invitation to a service as alice, and get its token.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        role: Role of the invitation.

    Returns:
        Token of the invitation.
    """
    response = invite(client, service_id, role)
    assert response.status_code == 201, response.get_json()
    token: str = response.get_json()["token"]
    return token


def accept(client: FlaskClient, token: Any, user: str | None = BOB) -> TestResponse:
    """Accept an invitation.

    Args:
        client: Test client of the app.
        token: Token of the invitation (`token` of the request body).
        user: User who accepts it; no token if `None`.

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/invitations/accept", json={"token": token}, headers=headers(user)
    )


def invitations(
    client: FlaskClient, service_id: str, user: str = ALICE
) -> list[dict[str, Any]]:
    """List the invitations to a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who lists them.

    Returns:
        Invitations, as listed.
    """
    response = client.get(
        f"{BASE}/services/{service_id}/invitations", headers=headers(user)
    )
    assert response.status_code == 200, response.get_json()
    listed: list[dict[str, Any]] = response.get_json()
    return listed


def revoke_invitation(
    client: FlaskClient, service_id: str, invitation_id: str, user: str = ALICE
) -> TestResponse:
    """Revoke an invitation to a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        invitation_id: Invitation identifier.
        user: User who revokes it.

    Returns:
        Response.
    """
    return client.delete(
        f"{BASE}/services/{service_id}/invitations/{invitation_id}",
        headers=headers(user),
    )


def stored(app: App) -> list[dict[str, Any]]:
    """Read the stored invitations.

    Args:
        app: App.

    Returns:
        Stored invitations, sorted by creation.
    """
    with app.app.app_context():
        return list(get_collection(INVITATIONS).find({}, sort=[("created_at", 1)]))


def set_invitations_config(app: App, **kwargs: Any) -> None:
    """Set options of `custom.access_control.invitations`.

    Args:
        app: The app.
        kwargs: Options and their values.
    """
    with app.app.app_context():
        config = get_custom_config(CustomConfig).access_control.invitations
        for key, value in kwargs.items():
            setattr(config, key, value)


def expire(app: App) -> None:
    """Let all stored invitations expire, before MongoDB deletes them.

    Args:
        app: App.
    """
    with app.app.app_context():
        get_collection(INVITATIONS).update_many(
            {}, {"$set": {"expires_at": now() - timedelta(seconds=1)}}
        )


def _parse(value: str) -> datetime:
    """Parse an RFC 3339 date-time in UTC, as served.

    Args:
        value: Date-time, e.g., `2026-10-20T12:34:56.789Z`.

    Returns:
        The time.
    """
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_invite_and_accept(client: FlaskClient) -> None:
    """Owners invite users, who get the role by accepting the invitation.

    The token is only returned on creation; the role is granted to the user
    who accepts, with their name, and owners see who accepted.
    """
    service_id = register(client)
    response = invite(client, service_id)
    assert response.status_code == 201, response.get_json()
    created = response.get_json()
    assert created["service_id"] == service_id
    assert created["role"] == "maintainer"
    assert created["created_by"] == ALICE
    assert len(created["token"]) >= TOKEN_BYTES
    expiry = _parse(created["expires_at"]) - _parse(created["created_at"])
    assert expiry == timedelta(days=7)
    listed = invitations(client, service_id)
    assert listed == [{k: v for k, v in created.items() if k != "token"}]
    assert update(client, service_id, BOB) == 403
    response = accept(client, created["token"])
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"service_id": service_id, "role": "maintainer"}
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "maintainer")]
    assert {"user_id": BOB, "name": NAMES[BOB], "role": "maintainer"} in (
        permission_list(client, service_id)
    )
    assert update(client, service_id, BOB) == 200
    (accepted,) = invitations(client, service_id)
    acceptance = accepted.pop("accepted")
    assert accepted == listed[0]
    assert acceptance["user_id"] == BOB
    assert acceptance["name"] == NAMES[BOB]
    assert "email" not in acceptance
    assert _parse(acceptance["accepted_at"]) >= _parse(created["created_at"])


def test_owner_invitation(client: FlaskClient) -> None:
    """Invitations as owner make the user an owner, replacing other roles."""
    service_id = register(client)
    assert grant(client, service_id, CAROL, "maintainer") == 200
    token = token_of(client, service_id, role="owner")
    assert accept(client, token, user=CAROL).status_code == 200
    assert permissions(client, service_id) == [(ALICE, "owner"), (CAROL, "owner")]
    assert delete(client, service_id, CAROL) == 200


def test_token_stored_as_hash(app: App, client: FlaskClient) -> None:
    """Only the SHA-256 hash of the token is stored."""
    service_id = register(client)
    response = invite(client, service_id)
    created = response.get_json()
    (document,) = stored(app)
    assert (
        document["token_hash"] == hashlib.sha256(created["token"].encode()).hexdigest()
    )
    assert document["token_hash"] == hash_token(created["token"])
    assert created["token"] not in repr(document)
    assert document["_id"] == created["id"]


def test_tokens_unique(client: FlaskClient) -> None:
    """Each invitation has its own identifier and token."""
    service_id = register(client)
    first, second = (invite(client, service_id).get_json() for _ in range(2))
    assert first["id"] != second["id"]
    assert first["token"] != second["token"]
    assert len(invitations(client, service_id)) == 2


def test_single_use(app: App, client: FlaskClient) -> None:
    """Invitations can be accepted once, also by the same user."""
    service_id = register(client)
    token = token_of(client, service_id)
    assert accept(client, token).status_code == 200
    for user in (BOB, ERIN):
        response = accept(client, token, user=user)
        assert response.status_code == 409
        assert response.get_json()["code"] == "invitation-accepted"
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "maintainer")]


def test_concurrent_accepts(client: FlaskClient) -> None:
    """Of concurrent acceptances of an invitation, only one succeeds."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("mongomock is not thread-safe")
    service_id = register(client)
    token = token_of(client, service_id)
    users = [f"user{index}@example.org" for index in range(8)]
    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        statuses = list(pool.map(lambda user: accept(client, token, user), users))
    codes = sorted(response.status_code for response in statuses)
    assert codes == [200] + [409] * (len(users) - 1)
    granted = [user for user, role in permissions(client, service_id) if user != ALICE]
    assert len(granted) == 1


def test_unknown_token(client: FlaskClient) -> None:
    """Unknown tokens are not found; invalid bodies are rejected."""
    register(client)
    assert accept(client, "x" * 43).status_code == 404
    for body in ({}, {"token": ""}, {"token": "x" * 257}, {"token": 1}):
        response = client.post(
            f"{BASE}/invitations/accept", json=body, headers=headers(BOB)
        )
        assert response.status_code == 400, body
    response = client.post(
        f"{BASE}/invitations/accept",
        json={"token": "x", "role": "owner"},
        headers=headers(BOB),
    )
    assert response.status_code == 400


def test_accept_requires_login(client: FlaskClient) -> None:
    """Accepting requires a token; so does managing invitations."""
    service_id = register(client)
    token = token_of(client, service_id)
    assert accept(client, token, user=None).status_code == 401
    path = f"{BASE}/services/{service_id}/invitations"
    assert client.post(path, json={"role": "owner"}).status_code == 401
    assert client.get(path).status_code == 401
    assert client.delete(f"{path}/x").status_code == 401


def test_expired(app: App, client: FlaskClient) -> None:
    """Expired invitations can't be accepted or revoked, and aren't listed.

    Also once accepted.
    """
    service_id = register(client)
    accepted = token_of(client, service_id)
    assert accept(client, accepted).status_code == 200
    token = token_of(client, service_id)
    (invitation_id,) = [i["id"] for i in invitations(client, service_id)][1:]
    expire(app)
    assert accept(client, token, user=ERIN).status_code == 404
    assert accept(client, accepted, user=ERIN).status_code == 404
    assert invitations(client, service_id) == []
    response = revoke_invitation(client, service_id, invitation_id)
    assert response.status_code == 404


def test_expiry_days(app: App, client: FlaskClient) -> None:
    """Invitations expire after the configured days."""
    set_invitations_config(app, expiry_days=1)
    service_id = register(client)
    created = invite(client, service_id).get_json()
    expiry = _parse(created["expires_at"]) - _parse(created["created_at"])
    assert expiry == timedelta(days=1)


def test_ttl_index(app: App, client: FlaskClient) -> None:
    """MongoDB deletes expired invitations (TTL index on `expires_at`)."""
    with app.app.app_context():
        indexes = get_collection(INVITATIONS).index_information()
    ttl = [index for index in indexes.values() if "expireAfterSeconds" in index]
    assert [(index["key"], index["expireAfterSeconds"]) for index in ttl] == [
        ([("expires_at", 1)], 0)
    ]
    unique = [index["key"] for index in indexes.values() if index.get("unique")]
    assert unique == [[("token_hash", 1)]]
    keys = [index["key"] for index in indexes.values()]
    assert [("service_id", 1), ("created_at", 1)] in keys


def test_revoke(app: App, client: FlaskClient) -> None:
    """Owners revoke open invitations, which can't be accepted then.

    Accepted invitations can't be revoked; unknown ones, and those of other
    services, are not found.
    """
    service_id = register(client)
    other_id = register(client)
    token = token_of(client, service_id)
    (invitation,) = invitations(client, service_id)
    assert revoke_invitation(client, other_id, invitation["id"]).status_code == 404
    response = revoke_invitation(client, service_id, invitation["id"])
    assert response.status_code == 200
    assert response.get_json() == invitation["id"]
    assert accept(client, token).status_code == 404
    assert revoke_invitation(client, service_id, invitation["id"]).status_code == 404
    assert invitations(client, service_id) == []
    token = token_of(client, service_id)
    assert accept(client, token).status_code == 200
    (accepted,) = invitations(client, service_id)
    response = revoke_invitation(client, service_id, accepted["id"])
    assert response.status_code == 409
    assert response.get_json()["code"] == "invitation-accepted"
    assert revoke_invitation(client, service_id, "unknown").status_code == 404
    assert len(stored(app)) == 1


def test_only_owners_manage(client: FlaskClient) -> None:
    """Only owners (also through groups) and admins manage invitations.

    Maintainers and other users can't create, list or revoke them.
    """
    service_id = register(client)
    assert grant(client, service_id, BOB, "maintainer") == 200
    (invitation,) = [invite(client, service_id).get_json()]
    path = f"{BASE}/services/{service_id}/invitations"
    for user in (BOB, CAROL):
        assert invite(client, service_id, user=user).status_code == 403
        assert client.get(path, headers=headers(user)).status_code == 403
        response = revoke_invitation(client, service_id, invitation["id"], user=user)
        assert response.status_code == 403
    # admins only in admin mode
    response = client.get(path, headers=headers(ADMIN, admin_mode=False))
    assert response.status_code == 403
    assert invite(client, service_id, user=ADMIN).status_code == 201
    assert len(invitations(client, service_id, user=ADMIN)) == 2
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    assert invite(client, service_id, user=ERIN).status_code == 201
    response = revoke_invitation(client, service_id, invitation["id"], user=ERIN)
    assert response.status_code == 200


def test_invalid_role(client: FlaskClient) -> None:
    """Invitations are for the roles on services only."""
    service_id = register(client)
    for body in ({"role": "admin"}, {}, {"role": "owner", "user_id": BOB}):
        response = client.post(
            f"{BASE}/services/{service_id}/invitations",
            json=body,
            headers=headers(ALICE),
        )
        assert response.status_code == 400, body


def test_unknown_service(client: FlaskClient) -> None:
    """Invitations to unknown services are not found."""
    assert invite(client, "UNKNWN", user=ADMIN).status_code == 404
    response = client.get(f"{BASE}/services/UNKNWN/invitations", headers=headers(ADMIN))
    assert response.status_code == 404
    assert revoke_invitation(client, "UNKNWN", "x", user=ADMIN).status_code == 404


def test_deleted_with_service(app: App, client: FlaskClient) -> None:
    """Invitations are deleted with their service."""
    service_id = register(client)
    other_id = register(client)
    token = token_of(client, service_id)
    token_of(client, other_id)
    assert delete(client, service_id, ALICE) == 200
    assert [i["service_id"] for i in stored(app)] == [other_id]
    assert accept(client, token).status_code == 404


def test_service_deleted_before_accept(app: App, client: FlaskClient) -> None:
    """Invitations left on a deleted service are not found, and kept."""
    service_id = register(client)
    token = token_of(client, service_id)
    with app.app.app_context():
        get_collection("services").delete_one({"id": service_id})
    assert accept(client, token).status_code == 404
    (document,) = stored(app)
    assert "accepted" not in document
    with app.app.app_context():
        assert list_roles(f"{PATH}/{service_id}") != []
        assert not get_collection(USER_NAMES).count_documents({"user_id": BOB})


def test_create_racing_revoke(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invitations stored while the creator's role is revoked are removed."""
    service_id = register(client)
    original = invitations_module._created_as
    results = iter([None, {"user": "someone-else"}])

    def created_as(serviceId: str, creator: str) -> dict[str, Any]:
        """Check the creator, then report another basis on the second call.

        Args:
            serviceId: Service identifier.
            creator: Creator.

        Returns:
            How the creator may create invitations, or another basis.
        """
        return next(results) or original(serviceId, creator)

    monkeypatch.setattr(invitations_module, "_created_as", created_as)
    assert invite(client, service_id).status_code == 403
    assert stored(app) == []


def test_create_racing_delete(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invitations created while their service is deleted are removed."""
    service_id = register(client)
    checks = iter([True, False])
    monkeypatch.setattr(invitations_module, "service_exists", lambda _: next(checks))
    assert invite(client, service_id).status_code == 404
    assert stored(app) == []


def test_last_owner_releases(app: App, client: FlaskClient) -> None:
    """Invitations that can't be accepted stay open.

    E.g., the last owner can't accept an invitation as maintainer, which
    others can still accept.
    """
    service_id = register(client)
    token = token_of(client, service_id)
    response = accept(client, token, user=ALICE)
    assert response.status_code == 409
    assert response.get_json()["code"] == "last-owner"
    assert "accepted" not in invitations(client, service_id)[0]
    assert accept(client, token).status_code == 200
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "maintainer")]


def test_release_keeps_other_acceptance(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed acceptance only releases its own claim."""
    service_id = register(client)
    token = token_of(client, service_id)

    def accepted_meanwhile(*args: Any, **kwargs: Any) -> None:
        """Let another user accept the invitation, then fail.

        Args:
            args: Arguments of `put_role()`; unused.
            kwargs: Keyword arguments of `put_role()`; unused.

        Raises:
            RuntimeError: Always (a server error).
        """
        with app.app.app_context():
            get_collection(INVITATIONS).update_one(
                {}, {"$set": {"accepted.user_id": ERIN}}
            )
        raise RuntimeError("failed")

    monkeypatch.setattr(invitations_module, "put_role", accepted_meanwhile)
    assert accept(client, token).status_code == 500
    (document,) = stored(app)
    assert document["accepted"]["user_id"] == ERIN


def test_limit(app: App, client: FlaskClient) -> None:
    """Services have at most the configured number of invitations.

    Open and accepted invitations count until they expire, so that owners
    can't grow the collection by accepting their own invitations; revoked
    and expired ones don't count; `None` lifts the limit.
    """
    set_invitations_config(app, max_per_service=2)
    service_id = register(client)
    other_id = register(client)
    first = token_of(client, service_id, role="owner")
    token_of(client, service_id)
    response = invite(client, service_id)
    assert response.status_code == 409
    assert response.get_json()["code"] == "invitations-limit"
    assert invite(client, other_id).status_code == 201
    assert accept(client, first, user=ALICE).status_code == 200
    assert invite(client, service_id).status_code == 409
    open_id = next(
        i["id"] for i in invitations(client, service_id) if "accepted" not in i
    )
    assert revoke_invitation(client, service_id, open_id).status_code == 200
    assert invite(client, service_id).status_code == 201
    assert invite(client, service_id).status_code == 409
    expire(app)
    assert invite(client, service_id).status_code == 201
    set_invitations_config(app, max_per_service=0)
    assert invite(client, service_id).status_code == 409
    set_invitations_config(app, max_per_service=None)
    for _ in range(3):
        assert invite(client, service_id).status_code == 201


def test_email_and_name(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptances have the name and email address that the IdP releases.

    Blank and non-string values are left out.
    """
    claims: dict[str, Any] = {"name": " Bob ", "email": "bob@example.org"}
    monkeypatch.setattr(invitations_module, "get_current_claims", lambda: claims)
    with app.app.app_context():
        acceptance = invitations_module._acceptance(BOB)
        assert acceptance["name"] == "Bob"
        assert acceptance["email"] == "bob@example.org"
        claims.update(name=" ", email=1)
        acceptance = invitations_module._acceptance(BOB)
    assert set(acceptance) == {"user_id", "accepted_at"}


def test_logged(client: FlaskClient, caplog: pytest.LogCaptureFixture) -> None:
    """Creations, revocations and acceptances are logged, without tokens."""
    caplog.set_level("INFO")
    service_id = register(client)
    created = invite(client, service_id).get_json()
    assert accept(client, created["token"]).status_code == 200
    other = invite(client, service_id).get_json()
    assert revoke_invitation(client, service_id, other["id"]).status_code == 200
    assert (
        f"Invitation '{created['id']}' to service '{service_id}' as 'maintainer' "
        f"created by '{ALICE}'." in caplog.text
    )
    assert (
        f"Invitation '{created['id']}' to service '{service_id}' as 'maintainer' "
        f"accepted by '{BOB}'." in caplog.text
    )
    assert (
        f"Invitation '{other['id']}' to service '{service_id}' revoked by "
        f"'{ALICE}'." in caplog.text
    )
    assert created["token"] not in caplog.text


def test_limit_logged(
    app: App, client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Invitations refused by the limit are logged."""
    caplog.set_level("INFO")
    set_invitations_config(app, max_per_service=0)
    service_id = register(client)
    assert invite(client, service_id).status_code == 409
    assert f"Invitation to service '{service_id}' not created" in caplog.text


def test_moved_with_rename(app: App, client: FlaskClient) -> None:
    """Invitations move with their service when it is renamed."""
    service_id = register(client)
    token = token_of(client, service_id)
    assert rename(client, service_id).status_code == 200
    (invitation,) = invitations(client, NEW_ID)
    assert invitation["service_id"] == NEW_ID
    assert accept(client, token).status_code == 200
    assert permissions(client, NEW_ID) == [(ALICE, "owner"), (BOB, "maintainer")]


def test_stale_removed_on_rename(app: App, client: FlaskClient) -> None:
    """Invitations left on the new identifier of a rename are removed.

    Those of the renamed service move there.
    """
    service_id = register(client)
    created = invite(client, service_id).get_json()
    with app.app.app_context():
        get_collection(INVITATIONS).insert_one(
            {
                "_id": "stale",
                "token_hash": "x",
                "service_id": NEW_ID,
                "role": "owner",
                "created_by": BOB,
                "created_at": now(),
                "expires_at": now() + timedelta(days=1),
            }
        )
    assert rename(client, service_id).status_code == 200
    assert [(i["_id"], i["service_id"]) for i in stored(app)] == [
        (created["id"], NEW_ID)
    ]


def test_resume_moves_invitations(app: App, client: FlaskClient) -> None:
    """Interrupted renames move the invitations when they are finished."""
    service_id = register(client)
    token_of(client, service_id)
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    resume(app)
    assert [i["service_id"] for i in stored(app)] == [NEW_ID]


def test_resume_removes_invitations(app: App, client: FlaskClient) -> None:
    """Invitations are removed if neither identifier of a rename exists."""
    service_id = register(client)
    token_of(client, service_id)
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    with app.app.app_context():
        get_collection("services").delete_one({"id": NEW_ID})
    resume(app)
    assert stored(app) == []


def test_revoked_owner_cannot_regain(app: App, client: FlaskClient) -> None:
    """Invitations of owners who lost the role are deleted, not accepted.

    Also those of owners downgraded to maintainer; others' stay valid.
    """
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    carols = invite(client, service_id, role="owner", user=CAROL).get_json()
    alices = token_of(client, service_id)
    assert revoke(client, service_id, CAROL) == 200
    assert accept(client, carols["token"], user=CAROL).status_code == 404
    assert [i["role"] for i in stored(app)] == ["maintainer"]
    assert accept(client, alices).status_code == 200
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "maintainer")]
    assert grant(client, service_id, CAROL, "owner") == 200
    token = invite(client, service_id, role="owner", user=CAROL).get_json()["token"]
    assert grant(client, service_id, CAROL, "maintainer") == 200
    assert accept(client, token, user=ERIN).status_code == 404


def test_revoked_owner_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Invitations deleted as their creator lost the role are logged."""
    caplog.set_level("INFO")
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    created = invite(client, service_id, user=CAROL).get_json()
    assert revoke(client, service_id, CAROL) == 200
    assert accept(client, created["token"]).status_code == 404
    assert (
        f"Invitation '{created['id']}' to service '{service_id}' deleted: its "
        f"creator '{CAROL}' is no longer an owner." in caplog.text
    )


def test_group_owner_invitations(app: App, client: FlaskClient) -> None:
    """Invitations of owners through a group hold while the group is owner."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    first = invite(client, service_id, user=ERIN).get_json()
    second = invite(client, service_id, user=ERIN).get_json()
    (document, _) = stored(app)
    assert document["created_as"] == {"group": TEAM_GROUP}
    assert "created_as" not in first
    assert accept(client, first["token"]).status_code == 200
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/groups/{quote(TEAM_GROUP, safe='')}",
        headers=headers(ALICE),
    )
    assert response.status_code == 200
    assert accept(client, second["token"], user=CAROL).status_code == 404


def test_admin_invitations(app: App, client: FlaskClient) -> None:
    """Invitations of admins don't depend on a role on the service."""
    service_id = register(client)
    created = invite(client, service_id, role="owner", user=ADMIN).get_json()
    assert stored(app)[0]["created_as"] == {"admin": True}
    assert accept(client, created["token"]).status_code == 200
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "owner")]
    assert stored(app)[0]["created_as"] == {"admin": True}


def test_owner_creator(app: App, client: FlaskClient) -> None:
    """Invitations of owners record the user, also if a group is owner too."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    invite(client, service_id)
    assert stored(app)[0]["created_as"] == {"user": ALICE}


def test_creator_not_owner(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating fails if the creator lost the role after authorization."""
    service_id = register(client)
    monkeypatch.setattr(invitations_module, "list_roles", lambda _: [])
    assert invite(client, service_id).status_code == 403
    assert stored(app) == []


def test_without_creator_basis(app: App, client: FlaskClient) -> None:
    """Invitations stored without how they were created grant no role."""
    service_id = register(client)
    token = token_of(client, service_id)
    with app.app.app_context():
        get_collection(INVITATIONS).update_many({}, {"$unset": {"created_as": ""}})
    assert accept(client, token).status_code == 404
    assert stored(app) == []


@pytest.mark.parametrize(
    "user", ["groups", "groups/x", "/x", "\\x", "a/../b", "a\\..", ".", "groups\\x"]
)
def test_unmanageable_user(app: App, client: FlaskClient, user: str) -> None:
    """Users whose roles the permissions API couldn't revoke can't accept."""
    service_id = register(client)
    token = token_of(client, service_id)
    assert not is_manageable_user_id(user)
    assert accept(client, token, user=user).status_code == 400
    assert "accepted" not in stored(app)[0]


@pytest.mark.parametrize(
    "user", [BOB, "https://login.example.org/|x", "a/b", "a.b/..c"]
)
def test_manageable_user(user: str) -> None:
    """Other identifiers, also with slashes and dots, are fine."""
    assert is_manageable_user_id(user)


def test_names_removed_with_role(client: FlaskClient) -> None:
    """Names in accepted invitations are removed with the user's roles."""
    service_id = register(client)
    other_id = register(client)
    for target in (service_id, other_id):
        assert accept(client, token_of(client, target)).status_code == 200
    assert invitations(client, service_id)[0]["accepted"]["name"] == NAMES[BOB]
    assert revoke(client, service_id, BOB) == 200
    (accepted,) = invitations(client, service_id)
    assert set(accepted["accepted"]) == {"user_id", "accepted_at"}
    assert invitations(client, other_id)[0]["accepted"]["name"] == NAMES[BOB]


def test_group_creator_left_group(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creators who left the owner group can't accept their invitations.

    Others still can, as long as the group is owner.
    """
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    own = invite(client, service_id, role="owner", user=ERIN).get_json()
    other = invite(client, service_id, user=ERIN).get_json()
    monkeypatch.setitem(GROUPS, ERIN, [])
    # user info is cached per token
    auth._userinfo_cache.clear()
    assert accept(client, own["token"], user=ERIN).status_code == 404
    assert accept(client, other["token"]).status_code == 200
    assert [i["_id"] for i in stored(app)] == [other["id"]]


def test_creator_checked_in_lock(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The creator is checked while the service is locked.

    E.g., an acceptance during a rename (which moves the roles before the
    invitations) waits for it, or fails without deleting the invitation.
    """
    service_id = register(client)
    token = token_of(client, service_id)
    calls: list[str] = []
    original = invitations_module._creator_may_grant

    def checked(invitation: dict[str, Any], user_id: str) -> bool:
        """Record that the check runs inside the lock, then check.

        Args:
            invitation: Stored invitation.
            user_id: Accepting user.

        Returns:
            Result of the original check.
        """
        with app.app.app_context():
            locked = get_collection(LOCKS).count_documents(
                {"service": service_id}, limit=1
            )
        calls.append("locked" if locked else "unlocked")
        return original(invitation, user_id)

    monkeypatch.setattr(invitations_module, "_creator_may_grant", checked)
    make_lock(app, service_id, age=timedelta(0))
    monkeypatch.setattr(locks_module, "LOCK_WAIT", 0)
    monkeypatch.setattr(locks_module, "LOCK_ATTEMPTS", 1)
    response = accept(client, token)
    assert response.status_code == 409
    assert response.get_json()["code"] == "service-busy"
    assert calls == []
    (document,) = stored(app)
    assert "accepted" not in document
    with app.app.app_context():
        get_collection(LOCKS).delete_many({})
    assert accept(client, token).status_code == 200
    assert calls == ["locked"]


def test_names_hidden_without_role(app: App, client: FlaskClient) -> None:
    """Names of users without a role on the service are not served.

    Also if removing them failed when the role was revoked.
    """
    service_id = register(client)
    assert accept(client, token_of(client, service_id)).status_code == 200
    with app.app.app_context():
        remove_resource_role(BOB, "maintainer", f"{PATH}/{service_id}")
    (accepted,) = invitations(client, service_id)
    assert set(accepted["accepted"]) == {"user_id", "accepted_at"}
    assert stored(app)[0]["accepted"]["name"] == NAMES[BOB]
