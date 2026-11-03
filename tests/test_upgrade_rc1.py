"""Tests for upgrades of data stored by 1.0.0rc1 to the paths of users' roles.

`data/store_1.0.0rc1.json` is what 1.0.0rc1 stored in MongoDB 7.0, except for
the hashes of the tokens of its invitations, so that no such value is in the
repository; `data/capture_rc1.py` makes it and describes its data. The version
had the rule `/ga4gh/registry/v1/services/:id/permissions/*`, which covers the
roles of users and groups.

Each test starts the app of this version on the stored data, without a
migration.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bson import json_util
from flask.testing import FlaskClient
from foca.security.access_control.constants import CONFIGURED_FIELD
from pymongo import MongoClient
from pymongo.errors import OperationFailure
import pytest

from cloud_registry.ga4gh.registry.invitations import hash_token
from tests.conftest import TEAM_GROUP
from tests.mock_data import DB
from tests.test_access_control import (
    ALICE,
    BASE,
    BOB,
    CAROL,
    ERIN,
    NO_GROUPS,
    USERS_RULE_RC1,
    _create_app,
    grant,
    grant_group,
    group_path,
    headers,
    permission_list,
    revoke,
    revoke_group,
)
from tests.test_invitations import accept, invitations, revoke_invitation

STORE = Path(__file__).parent / "data" / "store_1.0.0rc1.json"
CAPTURE = json_util.loads(STORE.read_text())
SERVICE, GROUP_SERVICE = CAPTURE["services"]
NAMESPACED_USER = "https://login.other.example.org/|dave"
# Rule of 1.0.0rc1 in stored form, and the path of the rule that replaces it
RC1_RULE = {"ptype": "p", "v0": "owner", "v1": USERS_RULE_RC1[2]}
USERS_RULE_PATH = f"{BASE}/services/:id/permissions/users/*"
# Tokens of the invitations in the tests (cf. `stored()`)
INVITATION = "rc1"
EXPIRED_INVITATION = "rc1-expired"
ACCEPTED_INVITATION = "rc1-accepted"
# Filters for each of the invitations (cf. `data/capture_rc1.py`)
OPEN_QUERY = {"role": "maintainer", "accepted": {"$exists": False}}
EXPIRED_QUERY = {"role": "owner"}
ACCEPTED_QUERY = {"accepted": {"$exists": True}}
# Who accepted the accepted invitation
GRACE = "grace@example.org"


@pytest.fixture
def stored(
    db_client: MongoClient[dict[str, Any]], app_env: None
) -> MongoClient[dict[str, Any]]:
    """Store the data of 1.0.0rc1, with its invitations in their states.

    Their times are set relative to now: the open and accepted ones haven't
    expired, and the expired one, which is for the role `owner`, has.

    Returns:
        Client of the MongoDB with the data.
    """
    for name, documents in CAPTURE["store"].items():
        db_client[DB][name].insert_many(deepcopy(documents))
    stored_invitations = db_client[DB]["invitations"]
    now = datetime.now(UTC)
    for query, token, expires_at in (
        (OPEN_QUERY, INVITATION, now + timedelta(days=1)),
        (EXPIRED_QUERY, EXPIRED_INVITATION, now - timedelta(hours=1)),
        (ACCEPTED_QUERY, ACCEPTED_INVITATION, now + timedelta(days=1)),
    ):
        result = stored_invitations.update_one(
            query,
            {"$set": {"token_hash": hash_token(token), "expires_at": expires_at}},
        )
        assert result.matched_count == 1
    return db_client


@pytest.fixture
def upgraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored: MongoClient[dict[str, Any]],
) -> FlaskClient:
    """Test client of this version, started on the data of 1.0.0rc1."""
    return _create_app(monkeypatch, tmp_path)


def _update(client: FlaskClient, service_id: str, user: str) -> int:
    """Replace a stored service with itself, as a user.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who replaces the service.

    Returns:
        Status code of the response.
    """
    service = client.get(f"{BASE}/services/{service_id}").get_json()
    body = {
        key: service[key] for key in ("name", "organization", "type", "url", "version")
    }
    url = f"{BASE}/services/{service_id}"
    return client.put(url, json=body, headers=headers(user)).status_code


def test_configured_rule_replaced(
    stored: MongoClient[dict[str, Any]], upgraded: FlaskClient
) -> None:
    """The stored rule of 1.0.0rc1 is gone; roles are stored unchanged.

    Starting replaces the configured rules (FOCA's generations), so no rule of
    1.0.0rc1 lingers that would still grant anything.
    """
    rules = stored[DB]["policy_rules"]
    assert rules.count_documents(RC1_RULE) == 0
    assert (
        rules.count_documents({"ptype": "p", "v0": "owner", "v1": USERS_RULE_PATH}) == 1
    )
    expected = sorted(
        (
            rule
            for rule in CAPTURE["store"]["policy_rules"]
            if rule.get("ptype") == "g2"
        ),
        key=lambda rule: rule["id"],
    )
    roles = sorted(rules.find({"ptype": "g2"}), key=lambda rule: rule["id"])
    assert roles == expected


def test_roles_listed(upgraded: FlaskClient) -> None:
    """Roles of users and groups stored by 1.0.0rc1 are listed as before."""
    listed = permission_list(upgraded, SERVICE)
    assert {"user_id": ALICE, "name": "Alice Example", "role": "owner"} in listed
    assert {"user_id": BOB, "name": "Bob Example", "role": "maintainer"} in listed
    assert {"user_id": CAROL, "role": "owner"} in listed
    assert {"user_id": NAMESPACED_USER, "role": "maintainer"} in listed
    assert {"group": TEAM_GROUP, "role": "maintainer"} in listed


def test_roles_apply(upgraded: FlaskClient) -> None:
    """Stored roles of users and groups still allow what they did."""
    assert _update(upgraded, SERVICE, ALICE) == 200
    assert _update(upgraded, SERVICE, CAROL) == 200
    assert _update(upgraded, SERVICE, BOB) == 200
    # by the stored role of the group
    assert _update(upgraded, SERVICE, ERIN) == 200
    assert _update(upgraded, GROUP_SERVICE, ERIN) == 200
    assert _update(upgraded, GROUP_SERVICE, BOB) == 403
    # alice is a member of the group
    assert _update(upgraded, GROUP_SERVICE, ALICE) == 200


def test_stored_owners_change_roles(upgraded: FlaskClient) -> None:
    """Owners by stored roles manage users and groups on the new paths.

    Also the users stored by 1.0.0rc1, with slashes in their identifiers.
    """
    assert grant(upgraded, SERVICE, ERIN, "maintainer", CAROL) == 200
    assert revoke(upgraded, SERVICE, ERIN, ALICE) == 200
    url = (
        f"{BASE}/services/{SERVICE}/permissions/users/{quote(NAMESPACED_USER, safe='')}"
    )
    assert upgraded.delete(url, headers=headers(ALICE)).status_code == 200
    assert revoke_group(upgraded, SERVICE, TEAM_GROUP, CAROL) == 200
    assert grant_group(upgraded, SERVICE, TEAM_GROUP, "maintainer", ALICE) == 200
    # by the stored role of the group
    assert grant(upgraded, GROUP_SERVICE, BOB, "maintainer", ERIN) == 200
    assert _update(upgraded, GROUP_SERVICE, BOB) == 200
    # the stored last owner is kept
    assert revoke_group(upgraded, GROUP_SERVICE, TEAM_GROUP, ERIN) == 409


def test_old_paths_gone(upgraded: FlaskClient) -> None:
    """The paths of 1.0.0rc1 are not found, also for users with stored roles."""
    for user in (BOB, NAMESPACED_USER):
        url = f"{BASE}/services/{SERVICE}/permissions/{user}"
        response = upgraded.put(url, json={"role": "owner"}, headers=headers(ALICE))
        assert response.status_code == 404
        assert upgraded.delete(url, headers=headers(ALICE)).status_code == 404
    assert {"user_id": BOB, "name": "Bob Example", "role": "maintainer"} in (
        permission_list(upgraded, SERVICE)
    )


def test_stored_invitation_accepted(upgraded: FlaskClient) -> None:
    """An invitation stored by 1.0.0rc1 grants its role on the new layout."""
    assert _update(upgraded, SERVICE, "frank@example.org") == 403
    response = accept(upgraded, INVITATION, user="frank@example.org")
    assert response.status_code == 200, response.get_json()
    assert _update(upgraded, SERVICE, "frank@example.org") == 200
    # their role is managed on the new paths
    assert revoke(upgraded, SERVICE, "frank@example.org", ALICE) == 200


def _invitation_id(client: MongoClient[dict[str, Any]], query: dict[str, Any]) -> str:
    """Get the identifier of a stored invitation.

    Args:
        client: Client of the MongoDB with the data.
        query: Filter for the invitation.

    Returns:
        Identifier of the invitation.
    """
    invitation = client[DB]["invitations"].find_one(query)
    assert invitation is not None
    identifier: str = invitation["_id"]
    return identifier


def test_stored_invitations_listed(upgraded: FlaskClient) -> None:
    """Invitations stored by 1.0.0rc1 are listed with their status."""
    listed = invitations(upgraded, SERVICE)
    assert sorted(item["status"] for item in listed) == ["expired", "open"]
    everything = invitations(upgraded, SERVICE, include_accepted=True)
    assert sorted(item["status"] for item in everything) == [
        "accepted",
        "expired",
        "open",
    ]
    accepted = next(item for item in everything if item["status"] == "accepted")
    assert accepted["accepted"]["user_id"] == GRACE
    assert accepted["accepted"]["name"] == "Grace"
    assert accepted["accepted"]["email"] == GRACE
    assert {item["created_by"] for item in everything} == {ALICE}


def test_stored_expired_invitation(
    stored: MongoClient[dict[str, Any]], upgraded: FlaskClient
) -> None:
    """An expired invitation of 1.0.0rc1 can't be accepted, only be removed."""
    assert accept(upgraded, EXPIRED_INVITATION, user=ERIN).status_code == 404
    identifier = _invitation_id(stored, EXPIRED_QUERY)
    assert revoke_invitation(upgraded, SERVICE, identifier).status_code == 200
    assert revoke_invitation(upgraded, SERVICE, identifier).status_code == 404


def test_stored_accepted_invitation(
    stored: MongoClient[dict[str, Any]], upgraded: FlaskClient
) -> None:
    """An accepted invitation of 1.0.0rc1 can neither be reused nor revoked."""
    response = accept(upgraded, ACCEPTED_INVITATION, user=ERIN)
    assert response.status_code == 409
    assert response.get_json()["code"] == "invitation-accepted"
    identifier = _invitation_id(stored, ACCEPTED_QUERY)
    response = revoke_invitation(upgraded, SERVICE, identifier)
    assert response.status_code == 409
    assert response.get_json()["code"] == "invitation-accepted"


def test_stored_acceptance_forgotten(
    stored: MongoClient[dict[str, Any]], upgraded: FlaskClient
) -> None:
    """Revoking the role removes the name and email of an acceptance of 1.0.0rc1."""
    assert revoke(upgraded, SERVICE, GRACE, ALICE) == 200
    invitation = stored[DB]["invitations"].find_one({"accepted.user_id": GRACE})
    assert invitation is not None
    assert set(invitation["accepted"]) == {"user_id", "accepted_at"}


def test_going_back_index(
    stored: MongoClient[dict[str, Any]], upgraded: FlaskClient
) -> None:
    """1.0.0rc1 can't create its index until `expires_at_1` is dropped.

    The sequence of "Going back" in `docs/upgrading.md`, with the index as
    1.0.0rc1 configures it.
    """
    collection = stored[DB]["invitations"]
    keys = [("expires_at", 1)]
    assert collection.index_information()["expires_at_1"]["expireAfterSeconds"] > 0
    with pytest.raises(OperationFailure):
        collection.create_index(keys, expireAfterSeconds=0)
    collection.drop_index("expires_at_1")
    collection.create_index(keys, expireAfterSeconds=0)
    indexes = collection.index_information()
    assert indexes["expires_at_1"]["expireAfterSeconds"] == 0
    assert "accepted_expires_at" in indexes


@pytest.mark.parametrize("method", ["put", "delete"])
def test_overlay_without_group_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored: MongoClient[dict[str, Any]],
    method: str,
) -> None:
    """Without the rule of groups, the stored rule of 1.0.0rc1 allows nothing.

    It would allow owners to change roles of groups, if it had stayed.
    """
    overlay = {"security": {"access_control": {"policies": NO_GROUPS}}}
    client = _create_app(monkeypatch, tmp_path, overlay)
    assert stored[DB]["policy_rules"].count_documents(RC1_RULE) == 0
    response = getattr(client, method)(
        group_path(SERVICE, TEAM_GROUP),
        json={"role": "owner"},
        headers=headers(ALICE),
    )
    assert response.status_code == 403
    # roles of users aren't affected
    assert grant(client, SERVICE, ERIN, "maintainer") == 200


def test_rule_stored_via_permissions_api_stays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored: MongoClient[dict[str, Any]],
) -> None:
    """Rules stored via FOCA's permissions API stay, as documented.

    Stored here as that API stores it, without marking it as configured. Only
    configured rules are replaced, so admins must remove such rules
    themselves (cf. "From 1.0.0rc1" in `docs/upgrading.md`).
    """
    rules = stored[DB]["policy_rules"]
    rule = rules.find_one(RC1_RULE)
    assert rule is not None
    rules.insert_one(
        {
            key: value
            for key, value in rule.items()
            if key not in ("_id", "generations", CONFIGURED_FIELD)
        }
        | {"id": "viaApi"}
    )
    overlay = {"security": {"access_control": {"policies": NO_GROUPS}}}
    client = _create_app(monkeypatch, tmp_path, overlay)
    assert [found["id"] for found in rules.find(RC1_RULE)] == ["viaApi"]
    assert (
        client.delete(
            group_path(SERVICE, TEAM_GROUP), headers=headers(ALICE)
        ).status_code
        == 200
    )
