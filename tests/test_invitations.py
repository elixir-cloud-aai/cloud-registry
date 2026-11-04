"""Tests for invitations to services, with the app created from its config.

The app and its fixtures are set up in `tests/conftest.py`; the helpers for
services and roles are those of `tests/test_access_control.py` and
`tests/test_rename.py`.
"""

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
from threading import Barrier
import time
from typing import Any
from urllib.parse import quote

from connexion import App
from flask.testing import FlaskClient
from foca import get_custom_config
from foca.security import auth
from foca.security.access_control import list_roles, remove_resource_role
from pymongo import MongoClient
import pytest
from werkzeug.test import TestResponse

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry import invitations as invitations_module
from cloud_registry.ga4gh.registry import locks as locks_module
from cloud_registry.ga4gh.registry import permissions as permissions_module
from cloud_registry.ga4gh.registry.invitations import (
    SECONDS_PER_DAY,
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
from tests.conftest import ADMIN, CONFIG_OVERLAY, GROUPS, NAMES, TEAM_GROUP
from tests.mock_data import DB
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
    client: FlaskClient,
    service_id: str,
    role: str = "maintainer",
    user: str = ALICE,
    **fields: Any,
) -> TestResponse:
    """Create an invitation to a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        role: Role of the invitation.
        user: User who creates the invitation.
        **fields: Further fields of the request body (e.g., `label`).

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/services/{service_id}/invitations",
        json={"role": role, **fields},
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
    client: FlaskClient,
    service_id: str,
    user: str = ALICE,
    includeAccepted: bool = False,
) -> list[dict[str, Any]]:
    """List the invitations to a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who lists them.
        includeAccepted: Whether to ask for accepted invitations too.

    Returns:
        Invitations, as listed.
    """
    response = client.get(
        f"{BASE}/services/{service_id}/invitations",
        headers=headers(user),
        query_string={"includeAccepted": "true"} if includeAccepted else None,
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
        return list(get_collection(INVITATIONS).find({}, sort=[("createdAt", 1)]))


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


def expire(app: App, query: dict[str, Any] | None = None) -> None:
    """Let stored invitations expire, before MongoDB may delete them.

    Args:
        app: App.
        query: Filter for the invitations; all of them if `None`.
    """
    with app.app.app_context():
        get_collection(INVITATIONS).update_many(
            query or {}, {"$set": {"expiresAt": now() - timedelta(seconds=1)}}
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
    assert created["serviceId"] == service_id
    assert created["role"] == "maintainer"
    assert created["createdBy"] == ALICE
    assert len(created["token"]) >= TOKEN_BYTES
    expiry = _parse(created["expiresAt"]) - _parse(created["createdAt"])
    assert expiry == timedelta(days=7)
    listed = invitations(client, service_id)
    assert listed == [{k: v for k, v in created.items() if k != "token"}]
    assert update(client, service_id, BOB) == 403
    response = accept(client, created["token"])
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"serviceId": service_id, "role": "maintainer"}
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "maintainer")]
    assert {"userId": BOB, "name": NAMES[BOB], "role": "maintainer"} in (
        permission_list(client, service_id)
    )
    assert update(client, service_id, BOB) == 200
    assert invitations(client, service_id) == []
    (accepted,) = invitations(client, service_id, includeAccepted=True)
    acceptance = accepted.pop("accepted")
    assert accepted.pop("status") == "accepted"
    assert listed[0].pop("status") == "open"
    assert accepted == listed[0]
    assert acceptance["userId"] == BOB
    assert acceptance["name"] == NAMES[BOB]
    assert "email" not in acceptance
    assert _parse(acceptance["acceptedAt"]) >= _parse(created["createdAt"])


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
        document["tokenHash"] == hashlib.sha256(created["token"].encode()).hexdigest()
    )
    assert document["tokenHash"] == hash_token(created["token"])
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


def test_expired_open(app: App, client: FlaskClient) -> None:
    """Expired invitations that weren't used stay listed, but can't be used.

    They have the status `expired`, keep their label, and are refused as
    unknown tokens are. Being refused doesn't change them.
    """
    service_id = register(client)
    created = invite(client, service_id, label="For Bob").get_json()
    assert created["status"] == "open"
    expire(app)
    response = accept(client, created["token"])
    assert response.status_code == 404
    assert response.get_json()["status"] == 404
    (listed,) = invitations(client, service_id)
    assert listed["id"] == created["id"]
    assert listed["status"] == "expired"
    assert listed["label"] == "For Bob"
    assert "accepted" not in listed
    assert "accepted" not in stored(app)[0]
    assert permissions(client, service_id) == [(ALICE, "owner")]


def test_expired_at_expiry(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invitations are expired from the moment of `expiresAt`.

    So that the status agrees with what can be done with them.
    """
    service_id = register(client)
    token = token_of(client, service_id)
    moment = now()
    with app.app.app_context():
        get_collection(INVITATIONS).update_many({}, {"$set": {"expiresAt": moment}})
    monkeypatch.setattr(invitations_module, "now", lambda: moment)
    assert invitations(client, service_id)[0]["status"] == "expired"
    assert accept(client, token).status_code == 404


def test_listings_without_tokens(app: App, client: FlaskClient) -> None:
    """No token or hash of one is ever listed, whatever the status."""
    service_id = register(client)
    used = invite(client, service_id).get_json()
    assert accept(client, used["token"]).status_code == 200
    old = invite(client, service_id).get_json()
    expire(app, {"accepted": {"$exists": False}})
    fresh = invite(client, service_id).get_json()
    listed = invitations(client, service_id, includeAccepted=True)
    assert [i["status"] for i in listed] == ["accepted", "expired", "open"]
    text = repr(listed)
    for invitation, document in zip((used, old, fresh), stored(app), strict=True):
        assert invitation["token"] not in text
        assert document["tokenHash"] not in text
    assert all("token" not in i and "tokenHash" not in i for i in listed)


def test_accepted_not_listed_by_default(app: App, client: FlaskClient) -> None:
    """Accepted invitations are only listed if asked for, until they expire.

    Also then, they count and can't be used; once expired, they are hidden and
    not found, as they are about to be removed.
    """
    service_id = register(client)
    used = invite(client, service_id).get_json()
    assert accept(client, used["token"]).status_code == 200
    assert invitations(client, service_id) == []
    (listed,) = invitations(client, service_id, includeAccepted=True)
    assert listed["status"] == "accepted"
    assert listed["accepted"]["userId"] == BOB
    response = accept(client, used["token"], user=ERIN)
    assert response.status_code == 409
    assert response.get_json()["code"] == "invitation-accepted"
    expire(app)
    assert invitations(client, service_id) == []
    assert invitations(client, service_id, includeAccepted=True) == []
    assert accept(client, used["token"], user=ERIN).status_code == 404
    response = revoke_invitation(client, service_id, used["id"])
    assert response.status_code == 404


def test_includeAccepted_lists_all(app: App, client: FlaskClient) -> None:
    """With accepted invitations, the list has all that aren't hidden."""
    service_id = register(client)
    assert accept(client, token_of(client, service_id)).status_code == 200
    token_of(client, service_id)
    expire(app, {"accepted": {"$exists": False}})
    token_of(client, service_id)
    default = invitations(client, service_id)
    assert [i["status"] for i in default] == ["expired", "open"]
    everything = invitations(client, service_id, includeAccepted=True)
    assert [i["status"] for i in everything] == ["accepted", "expired", "open"]


def test_includeAccepted_invalid(client: FlaskClient) -> None:
    """Invalid values of `includeAccepted` are rejected."""
    service_id = register(client)
    response = client.get(
        f"{BASE}/services/{service_id}/invitations",
        headers=headers(ALICE),
        query_string={"includeAccepted": "maybe"},
    )
    assert response.status_code == 400


def test_delete_expired(app: App, client: FlaskClient) -> None:
    """Expired invitations that weren't used can be removed."""
    service_id = register(client)
    other_id = register(client)
    created = invite(client, service_id).get_json()
    expire(app)
    response = revoke_invitation(client, other_id, created["id"])
    assert response.status_code == 404
    response = revoke_invitation(client, service_id, created["id"])
    assert response.status_code == 200
    assert response.get_json() == created["id"]
    assert stored(app) == []
    response = revoke_invitation(client, service_id, created["id"])
    assert response.status_code == 404


def test_release_after_expiry(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim released after the expiry leaves an expired invitation."""
    service_id = register(client)
    token = token_of(client, service_id)

    def expire_then_fail(*args: Any, **kwargs: Any) -> None:
        """Let the invitation expire, then fail.

        Args:
            args: Arguments of `put_role()`; unused.
            kwargs: Keyword arguments of `put_role()`; unused.

        Raises:
            RuntimeError: Always (a server error).
        """
        expire(app)
        raise RuntimeError("failed")

    monkeypatch.setattr(invitations_module, "put_role", expire_then_fail)
    assert accept(client, token).status_code == 500
    monkeypatch.undo()
    (listed,) = invitations(client, service_id)
    assert listed["status"] == "expired"
    assert "accepted" not in stored(app)[0]


def test_expired_follow_service(app: App, client: FlaskClient) -> None:
    """Expired invitations move and are deleted with their service."""
    service_id = register(client)
    token_of(client, service_id)
    expire(app)
    assert rename(client, service_id).status_code == 200
    (listed,) = invitations(client, NEW_ID)
    assert listed["serviceId"] == NEW_ID
    assert listed["status"] == "expired"
    assert delete(client, NEW_ID, ALICE) == 200
    assert stored(app) == []


def _after(barrier: Barrier, function: Callable[..., TestResponse], *args: Any) -> Any:
    """Wait for the other threads, then send a request.

    Args:
        barrier: Barrier of the threads.
        function: Function that sends the request.
        *args: Arguments of the function.

    Returns:
        Response.
    """
    barrier.wait()
    return function(*args)


def test_concurrent_accept_and_revoke(client: FlaskClient) -> None:
    """Either an acceptance or the revocation of an invitation succeeds."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("mongomock is not thread-safe")
    service_id = register(client)
    for _ in range(5):
        created = invite(client, service_id).get_json()
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepting = pool.submit(_after, barrier, accept, client, created["token"])
            revoking = pool.submit(
                _after, barrier, revoke_invitation, client, service_id, created["id"]
            )
        outcome = (accepting.result().status_code, revoking.result().status_code)
        assert outcome in ((200, 409), (404, 200))
        if outcome[0] == 200:
            assert revoke(client, service_id, BOB) == 200


def test_concurrent_accepts_of_expired(app: App, client: FlaskClient) -> None:
    """Concurrent acceptances of an expired invitation all fail."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("mongomock is not thread-safe")
    service_id = register(client)
    token = token_of(client, service_id)
    expire(app)
    users = [f"user{index}@example.org" for index in range(8)]
    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        responses = list(pool.map(lambda user: accept(client, token, user), users))
    assert {response.status_code for response in responses} == {404}
    assert permissions(client, service_id) == [(ALICE, "owner")]


def test_expiry_days(app: App, client: FlaskClient) -> None:
    """Invitations expire after the configured days."""
    set_invitations_config(app, expiry_days=1)
    service_id = register(client)
    created = invite(client, service_id).get_json()
    expiry = _parse(created["expiresAt"]) - _parse(created["createdAt"])
    assert expiry == timedelta(days=1)


def _ttl_indexes(collection: Any) -> dict[str, tuple[Any, ...]]:
    """Get the TTL indexes of a collection.

    Args:
        collection: Collection.

    Returns:
        Keys, seconds and the partial filter of the TTL indexes, by name.
    """
    return {
        name: (
            index["key"],
            index["expireAfterSeconds"],
            index.get("partialFilterExpression"),
        )
        for name, index in collection.index_information().items()
        if "expireAfterSeconds" in index
    }


def _expected_ttl_indexes(days: int) -> dict[str, tuple[Any, ...]]:
    """Get the TTL indexes of the collection of invitations.

    Args:
        days: Days to keep expired invitations.

    Returns:
        The indexes as `_ttl_indexes()` gets them.
    """
    return {
        "expiresAt_1": ([("expiresAt", 1)], days * SECONDS_PER_DAY, None),
        "acceptedExpiresAt": (
            [("expiresAt", 1)],
            0,
            {"accepted": {"$exists": True}},
        ),
    }


def _overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    """Add a config overlay that sets the days to keep expired invitations.

    Args:
        tmp_path: Directory for the overlay.
        monkeypatch: Fixture to set `FOCA_CONFIG_OVERLAY` with.
        days: Days to keep expired invitations.
    """
    path = tmp_path / f"days-{days}.yaml"
    path.write_text(
        f"custom:\n  access_control:\n    invitations:\n"
        f"      keep_expired_days: {days}\n"
    )
    monkeypatch.setenv(
        "FOCA_CONFIG_OVERLAY", os.pathsep.join([str(CONFIG_OVERLAY), str(path)])
    )


@pytest.mark.usefixtures("app_env")
def test_ttl_index_replaced(mongo_client: MongoClient[dict[str, Any]]) -> None:
    """The app replaces a TTL index with other seconds.

    Data is kept, and starting again changes nothing.
    """
    collection = mongo_client[DB][INVITATIONS]
    collection.create_index([("tokenHash", 1)], unique=True)
    collection.create_index([("serviceId", 1), ("createdAt", 1)])
    collection.create_index([("expiresAt", 1)], expireAfterSeconds=0)
    collection.insert_one({"_id": "kept", "expiresAt": now() + timedelta(days=1)})
    create_app()
    assert _ttl_indexes(collection) == _expected_ttl_indexes(30)
    assert collection.count_documents({"_id": "kept"}) == 1
    create_app()
    assert _ttl_indexes(collection) == _expected_ttl_indexes(30)


@pytest.mark.usefixtures("app_env")
@pytest.mark.parametrize("days", [0, 1, 365])
def test_keep_expired_days_changed(
    mongo_client: MongoClient[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    days: int,
) -> None:
    """Changing the days takes effect when the app starts, without other steps."""
    create_app()
    collection = mongo_client[DB][INVITATIONS]
    assert _ttl_indexes(collection) == _expected_ttl_indexes(30)
    _overlay(tmp_path, monkeypatch, days)
    create_app()
    assert _ttl_indexes(collection) == _expected_ttl_indexes(days)
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    create_app()
    assert _ttl_indexes(collection) == _expected_ttl_indexes(30)


@pytest.fixture
def fast_ttl_monitor(
    db_client: MongoClient[dict[str, Any]],
) -> Iterator[MongoClient[dict[str, Any]]]:
    """Let MongoDB check TTL indexes every second, not every minute."""
    if not os.environ.get("MONGODB_TEST_URI"):
        pytest.skip("TTL indexes of MongoDB")
    db_client.admin.command("setParameter", 1, ttlMonitorSleepSecs=1)
    yield db_client
    db_client.admin.command("setParameter", 1, ttlMonitorSleepSecs=60)


def _stored_ids(collection: Any) -> set[str]:
    """Get the identifiers of the stored documents.

    Args:
        collection: Collection.

    Returns:
        Identifiers.
    """
    return {document["_id"] for document in collection.find()}


def _wait_for_ids(collection: Any, expected: set[str]) -> set[str]:
    """Wait until the given documents are the only ones left, or give up.

    Args:
        collection: Collection.
        expected: Identifiers of the documents to wait for.

    Returns:
        Identifiers of the documents that are left.
    """
    for _ in range(40):
        if _stored_ids(collection) == expected:
            break
        time.sleep(0.5)
    return _stored_ids(collection)


@pytest.mark.usefixtures("app_env")
def test_ttl_removal(
    fast_ttl_monitor: MongoClient[dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MongoDB removes invitations as documented, after an upgrade too.

    Accepted ones when they expire; others only after the days to keep them,
    also when the days change on a deployment with stored invitations.
    """
    collection = fast_ttl_monitor[DB][INVITATIONS]
    collection.create_index([("expiresAt", 1)], expireAfterSeconds=0)
    create_app()
    current = now()
    accepted = {"accepted": {"userId": BOB, "acceptedAt": current}}
    documents: list[dict[str, Any]] = [
        {"_id": "open", "expiresAt": current + timedelta(days=1)},
        {"_id": "used", "expiresAt": current + timedelta(days=1), **accepted},
        {"_id": "used-expired", "expiresAt": current, **accepted},
        {"_id": "expired", "expiresAt": current - timedelta(days=2)},
        {"_id": "old", "expiresAt": current - timedelta(days=31)},
    ]
    # the hash of the token is unique
    collection.insert_many([{**doc, "tokenHash": doc["_id"]} for doc in documents])
    expected = {"open", "used", "expired"}
    assert _wait_for_ids(collection, expected) == expected
    _overlay(tmp_path, monkeypatch, 1)
    create_app()
    expected = {"open", "used"}
    assert _wait_for_ids(collection, expected) == expected


def test_indexes(app: App, client: FlaskClient) -> None:
    """Invitations are removed by TTL indexes, and found by token and service."""
    with app.app.app_context():
        collection = get_collection(INVITATIONS)
        assert _ttl_indexes(collection) == _expected_ttl_indexes(30)
        indexes = collection.index_information()
    unique = [index["key"] for index in indexes.values() if index.get("unique")]
    assert unique == [[("tokenHash", 1)]]
    keys = [index["key"] for index in indexes.values()]
    assert [("serviceId", 1), ("createdAt", 1)] in keys


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
    (accepted,) = invitations(client, service_id, includeAccepted=True)
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
    for body in ({"role": "admin"}, {}, {"role": "owner", "userId": BOB}):
        response = client.post(
            f"{BASE}/services/{service_id}/invitations",
            json=body,
            headers=headers(ALICE),
        )
        assert response.status_code == 400, body


def test_label(app: App, client: FlaskClient) -> None:
    """Labels are stored and returned on creation and in the list."""
    service_id = register(client)
    created = invite(client, service_id, label="Bob (Lab Ö) <b>").get_json()
    assert created["label"] == "Bob (Lab Ö) <b>"
    assert invitations(client, service_id)[0]["label"] == "Bob (Lab Ö) <b>"
    assert stored(app)[0]["label"] == "Bob (Lab Ö) <b>"


def test_label_optional(app: App, client: FlaskClient) -> None:
    """Labels are optional, and left out of invitations without one.

    This includes those stored before labels existed.
    """
    service_id = register(client)
    created = invite(client, service_id).get_json()
    assert "label" not in created
    assert "label" not in invitations(client, service_id)[0]
    assert "label" not in stored(app)[0]


def test_label_trimmed(client: FlaskClient) -> None:
    """Whitespace around labels is removed."""
    service_id = register(client)
    assert invite(client, service_id, label="  Bob  ").get_json()["label"] == "Bob"


def test_label_longest(client: FlaskClient) -> None:
    """Labels have at most `100` characters, whatever their encoded size."""
    service_id = register(client)
    assert invite(client, service_id, label="Ö" * 100).status_code == 201
    assert invite(client, service_id, label="x" * 101).status_code == 400


@pytest.mark.parametrize(
    "label",
    [
        "",
        " ",
        "\t",
        "Bob\n",
        "\nBob",
        "a\nb",
        "a\x00b",
        "a\x7fb",
        "a\x85b",
        f"a{chr(0x2028)}b",
        chr(0x200B),
        f"a{chr(0x202E)}b",
        f"a{chr(0x200F)}b",
        f"a{chr(0x061C)}b",
        chr(0x200E),
        f"a{chr(0x2066)}b",
        chr(0xFEFF),
        f"Bob{chr(0xD800)}",
        1,
        None,
    ],
)
def test_label_invalid(app: App, client: FlaskClient, label: Any) -> None:
    """Labels are plain text of one line, not blank; others are rejected."""
    service_id = register(client)
    assert invite(client, service_id, label=label).status_code == 400
    assert stored(app) == []


def test_label_joiners(client: FlaskClient) -> None:
    """Labels may have joiners, which names in some scripts need."""
    service_id = register(client)
    label = f"a{chr(0x200C)}b{chr(0x200D)}c"
    assert invite(client, service_id, label=label).get_json()["label"] == label


def test_label_hidden_from_accepting_user(client: FlaskClient) -> None:
    """Only those who may list invitations see their labels.

    Not the user who accepts it, and not other users.
    """
    service_id = register(client)
    created = invite(client, service_id, label="For Bob").get_json()
    response = accept(client, created["token"])
    assert response.status_code == 200
    assert "For Bob" not in response.get_data(as_text=True)
    path = f"{BASE}/services/{service_id}/invitations"
    for user in (BOB, CAROL):
        response = client.get(path, headers=headers(user))
        assert response.status_code == 403
        assert "For Bob" not in response.get_data(as_text=True)


def test_label_not_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Labels may name people, so they are not logged."""
    caplog.set_level("INFO")
    service_id = register(client)
    created = invite(client, service_id, label="For Bob").get_json()
    assert accept(client, created["token"]).status_code == 200
    other = invite(client, service_id, label="For Bob").get_json()
    invitations(client, service_id)
    assert revoke_invitation(client, service_id, other["id"]).status_code == 200
    assert "For Bob" not in caplog.text


def test_label_removed_with_role(app: App, client: FlaskClient) -> None:
    """Labels of accepted invitations are removed with the user's roles.

    They may name the user, and accepted invitations can't be revoked; others
    keep theirs.
    """
    service_id = register(client)
    created = invite(client, service_id, label="For Bob").get_json()
    assert accept(client, created["token"]).status_code == 200
    invite(client, service_id, label="For Carol")
    assert revoke(client, service_id, BOB) == 200
    first, second = invitations(client, service_id, includeAccepted=True)
    assert "label" not in first
    assert second["label"] == "For Carol"


def test_label_moves_with_rename(client: FlaskClient) -> None:
    """Labels stay with invitations when their service is renamed."""
    service_id = register(client)
    invite(client, service_id, label="For Bob")
    assert rename(client, service_id).status_code == 200
    assert invitations(client, NEW_ID)[0]["label"] == "For Bob"


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
    assert [i["serviceId"] for i in stored(app)] == [other_id]
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
        assert not get_collection(USER_NAMES).count_documents({"userId": BOB})


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
                {}, {"$set": {"accepted.userId": ERIN}}
            )
        raise RuntimeError("failed")

    monkeypatch.setattr(invitations_module, "put_role", accepted_meanwhile)
    assert accept(client, token).status_code == 500
    (document,) = stored(app)
    assert document["accepted"]["userId"] == ERIN


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
    assert any(i["status"] == "expired" for i in invitations(client, service_id))
    set_invitations_config(app, max_per_service=0)
    assert invite(client, service_id).status_code == 409
    set_invitations_config(app, max_per_service=None)
    for _ in range(3):
        assert invite(client, service_id).status_code == 201


def test_limit_explains_accepted(client: FlaskClient) -> None:
    """The error says that accepted invitations, which are hidden, count."""
    service_id = register(client)
    response = None
    for _ in range(21):
        response = invite(client, service_id)
    assert response is not None
    assert response.status_code == 409
    problem = response.get_json()
    assert problem["code"] == "invitations-limit"
    assert "20" in problem["detail"]
    assert "ccepted invitations count until they expire" in problem["detail"]
    assert "includeAccepted" in problem["detail"]


def test_email_and_name(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptances have the name and email address that the IdP releases.

    Blank and non-string values are left out.
    """
    claims: dict[str, Any] = {
        "name": " Bob ",
        "email": "bob@example.org",
        "email_verified": True,
    }
    monkeypatch.setattr(invitations_module, "get_current_claims", lambda: claims)
    with app.app.app_context():
        acceptance = invitations_module._acceptance(BOB)
        assert acceptance["name"] == "Bob"
        assert acceptance["email"] == "bob@example.org"
        claims.update(name=" ", email=1)
        acceptance = invitations_module._acceptance(BOB)
    assert set(acceptance) == {"userId", "acceptedAt"}


@pytest.mark.parametrize("verified", [None, False, "true", 1])
def test_email_unverified(
    app: App, monkeypatch: pytest.MonkeyPatch, verified: object
) -> None:
    """Email addresses are only stored if verified (`email_verified: true`).

    Users may be able to set unverified ones to any address, e.g., that of
    the invited user; names are stored anyway, for display only.
    """
    claims: dict[str, Any] = {"name": "Bob", "email": "bob@example.org"}
    if verified is not None:
        claims["email_verified"] = verified
    monkeypatch.setattr(invitations_module, "get_current_claims", lambda: claims)
    with app.app.app_context():
        acceptance = invitations_module._acceptance(BOB)
    assert acceptance["name"] == "Bob"
    assert "email" not in acceptance


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


def test_removal_logged(
    app: App, client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Removing an expired invitation is logged, not as revoking a usable one."""
    caplog.set_level("INFO")
    service_id = register(client)
    created = invite(client, service_id).get_json()
    expire(app)
    assert revoke_invitation(client, service_id, created["id"]).status_code == 200
    assert (
        f"Invitation '{created['id']}' to service '{service_id}' removed by "
        f"'{ALICE}'." in caplog.text
    )


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
    assert invitation["serviceId"] == NEW_ID
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
                "tokenHash": "x",
                "serviceId": NEW_ID,
                "role": "owner",
                "createdBy": BOB,
                "createdAt": now(),
                "expiresAt": now() + timedelta(days=1),
            }
        )
    assert rename(client, service_id).status_code == 200
    assert [(i["_id"], i["serviceId"]) for i in stored(app)] == [
        (created["id"], NEW_ID)
    ]


def test_resume_moves_invitations(app: App, client: FlaskClient) -> None:
    """Interrupted renames move the invitations when they are finished."""
    service_id = register(client)
    token_of(client, service_id)
    make_lock(app, service_id)
    move_service(app, service_id, NEW_ID)
    resume(app)
    assert [i["serviceId"] for i in stored(app)] == [NEW_ID]


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
    """Invitations of owners whose roles are revoked are gone.

    Those of owners downgraded to maintainer are deleted when accepted; others'
    stay valid.
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


def test_revoke_logs_cleared(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Invitations deleted with the roles are logged."""
    caplog.set_level("INFO")
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    created = invite(client, service_id, user=CAROL).get_json()
    assert revoke(client, service_id, CAROL) == 200
    assert (
        f"Invitation '{created['id']}' to service '{service_id}' deleted: the roles "
        f"of its creator '{CAROL}' were revoked." in caplog.text
    )


def test_revoke_keeps_accepted_meanwhile(
    app: App, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An invitation accepted while the roles are revoked stays."""
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    created = invite(client, service_id, user=CAROL).get_json()

    class Racing:
        """Collection in which invitations are accepted once they are found."""

        def __init__(self, name: str) -> None:
            """Use a collection.

            Args:
                name: Name of the collection.
            """
            self.collection = get_collection(name)
            self.delete_one = self.collection.delete_one

        def find(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            """Find invitations, then accept them.

            Args:
                *args: Arguments of `find()`.
                **kwargs: Keyword arguments of `find()`.

            Returns:
                The invitations as found.
            """
            found = list(self.collection.find(*args, **kwargs))
            self.collection.update_many({}, {"$set": {"accepted": {"userId": BOB}}})
            return found

    monkeypatch.setattr(permissions_module, "get_collection", Racing)
    with app.app.app_context():
        permissions_module._delete_created_invitations(service_id, CAROL)
    assert [document["_id"] for document in stored(app)] == [created["id"]]


def test_revoke_clears_created(app: App, client: FlaskClient) -> None:
    """Revoking a user's roles deletes the invitations they created.

    Open and expired ones, on that service only. Accepted ones hold another
    user's acceptance, and those of others stay valid.
    """
    service_id = register(client)
    other_id = register(client)
    for target in (service_id, other_id):
        assert grant(client, target, CAROL, "owner") == 200
    accepted = invite(client, service_id, user=CAROL).get_json()
    assert accept(client, accepted["token"]).status_code == 200
    expired = invite(client, service_id, user=CAROL).get_json()
    expire(app, {"_id": expired["id"]})
    open_invitation = invite(client, service_id, user=CAROL).get_json()
    others = invite(client, other_id, user=CAROL).get_json()
    alices = invite(client, service_id).get_json()
    assert revoke(client, service_id, CAROL) == 200
    assert {document["_id"] for document in stored(app)} == {
        accepted["id"],
        others["id"],
        alices["id"],
    }
    assert accept(client, open_invitation["token"], user=ERIN).status_code == 404
    assert accept(client, alices["token"], user=ERIN).status_code == 200


def test_revoke_clears_created_through_group(app: App, client: FlaskClient) -> None:
    """Also those created through a group that stays owner, otherwise valid."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    created = invite(client, service_id, user=ERIN).get_json()
    assert stored(app)[0]["createdAs"] == {"group": TEAM_GROUP}
    assert grant(client, service_id, ERIN, "maintainer") == 200
    assert revoke(client, service_id, ERIN) == 200
    assert stored(app) == []
    assert accept(client, created["token"]).status_code == 404


def test_failed_revoke_keeps_created(app: App, client: FlaskClient) -> None:
    """Invitations stay if no role was revoked: last owners, users without one."""
    service_id = register(client)
    alices = invite(client, service_id).get_json()
    admins = invite(client, service_id, user=ADMIN).get_json()
    assert revoke(client, service_id, ALICE) == 409
    assert revoke(client, service_id, ADMIN) == 404
    assert {document["_id"] for document in stored(app)} == {
        alices["id"],
        admins["id"],
    }
    assert accept(client, admins["token"]).status_code == 200


def test_revoke_clears_created_as_admin(app: App, client: FlaskClient) -> None:
    """Also those created as an admin, which don't depend on a role."""
    service_id = register(client)
    assert grant(client, service_id, ADMIN, "maintainer") == 200
    created = invite(client, service_id, user=ADMIN).get_json()
    assert stored(app)[0]["createdAs"] == {"admin": True}
    assert revoke(client, service_id, ADMIN) == 200
    assert stored(app) == []
    assert accept(client, created["token"]).status_code == 404


def test_downgraded_owner_invitations(app: App, client: FlaskClient) -> None:
    """Invitations of owners only downgraded stay listed, but can't be accepted.

    Also not by themselves, and accepting deletes them. Those created as an admin
    don't depend on the role.
    """
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    created = invite(client, service_id, user=CAROL).get_json()
    assert grant(client, service_id, CAROL, "maintainer") == 200
    assert invitations(client, service_id)[0]["status"] == "open"
    assert accept(client, created["token"], user=CAROL).status_code == 404
    assert stored(app) == []
    assert grant(client, service_id, ADMIN, "owner") == 200
    admins = invite(client, service_id, user=ADMIN).get_json()
    assert grant(client, service_id, ADMIN, "maintainer") == 200
    assert accept(client, admins["token"]).status_code == 200


def test_downgraded_owner_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Invitations deleted as their creator lost the role are logged."""
    caplog.set_level("INFO")
    service_id = register(client)
    assert grant(client, service_id, CAROL, "owner") == 200
    created = invite(client, service_id, user=CAROL).get_json()
    assert grant(client, service_id, CAROL, "maintainer") == 200
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
    assert document["createdAs"] == {"group": TEAM_GROUP}
    assert "createdAs" not in first
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
    assert stored(app)[0]["createdAs"] == {"admin": True}
    assert accept(client, created["token"]).status_code == 200
    assert permissions(client, service_id) == [(ALICE, "owner"), (BOB, "owner")]
    assert stored(app)[0]["createdAs"] == {"admin": True}


def test_owner_creator(app: App, client: FlaskClient) -> None:
    """Invitations of owners record the user, also if a group is owner too."""
    service_id = register(client)
    assert grant_group(client, service_id, TEAM_GROUP, "owner") == 200
    invite(client, service_id)
    assert stored(app)[0]["createdAs"] == {"user": ALICE}


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
        get_collection(INVITATIONS).update_many({}, {"$unset": {"createdAs": ""}})
    assert accept(client, token).status_code == 404
    assert stored(app) == []


@pytest.mark.parametrize("user", ["/x", "\\x", "a/../b", "a\\..", "."])
def test_unmanageable_user(app: App, client: FlaskClient, user: str) -> None:
    """Users whose roles the permissions API couldn't revoke can't accept."""
    service_id = register(client)
    token = token_of(client, service_id)
    assert not is_manageable_user_id(user)
    assert accept(client, token, user=user).status_code == 400
    assert "accepted" not in stored(app)[0]


@pytest.mark.parametrize(
    "user",
    [
        BOB,
        "https://login.example.org/|x",
        "a/b",
        "a.b/..c",
        "groups",
        "groups/x",
        "groups\\x",
    ],
)
def test_manageable_user(user: str) -> None:
    """Other identifiers, also with slashes and dots, are fine."""
    assert is_manageable_user_id(user)


@pytest.mark.parametrize("user", ["groups", "groups/x"])
def test_user_named_groups_accepts(client: FlaskClient, user: str) -> None:
    """Users starting with the segment `groups` can accept invitations.

    Their roles are managed under `.../permissions/users/`, as those of others.
    """
    service_id = register(client)
    token = token_of(client, service_id)
    assert accept(client, token, user=user).status_code == 200
    url = f"{BASE}/services/{service_id}/permissions/users/{user}"
    assert client.delete(url, headers=headers(ALICE)).status_code == 200


def test_names_removed_with_role(client: FlaskClient) -> None:
    """Names in accepted invitations are removed with the user's roles."""
    service_id = register(client)
    other_id = register(client)
    for target in (service_id, other_id):
        assert accept(client, token_of(client, target)).status_code == 200
    listed = invitations(client, service_id, includeAccepted=True)
    assert listed[0]["accepted"]["name"] == NAMES[BOB]
    assert revoke(client, service_id, BOB) == 200
    (accepted,) = invitations(client, service_id, includeAccepted=True)
    assert set(accepted["accepted"]) == {"userId", "acceptedAt"}
    listed = invitations(client, other_id, includeAccepted=True)
    assert listed[0]["accepted"]["name"] == NAMES[BOB]


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

    Nor are the labels of their invitations, which may name them. Also if
    removing them failed when the role was revoked.
    """
    service_id = register(client)
    created = invite(client, service_id, label="For Bob").get_json()
    assert accept(client, created["token"]).status_code == 200
    with app.app.app_context():
        remove_resource_role(BOB, "maintainer", f"{PATH}/{service_id}")
    (accepted,) = invitations(client, service_id, includeAccepted=True)
    assert set(accepted["accepted"]) == {"userId", "acceptedAt"}
    assert "label" not in accepted
    assert stored(app)[0]["accepted"]["name"] == NAMES[BOB]
