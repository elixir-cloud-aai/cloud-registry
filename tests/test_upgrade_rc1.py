"""Tests for upgrades of data stored by 1.0.0rc1 to the paths of users' roles.

`data/store_1.0.0rc1.json` is what 1.0.0rc1 stored in MongoDB 7.0, except for
the hash of the token of its invitation, so that no such value is in the
repository. The version had the rule
`/ga4gh/registry/v1/services/:id/permissions/*`, which covers the roles of
users and groups. In the store are the service `R71YIG` of alice, with roles of
users (bob, carol and a namespaced user) and of a group (maintainer), and an open
invitation, and the service `6WHRT0`, whose only owner is the group.

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
from tests.test_invitations import accept

STORE = Path(__file__).parent / "data" / "store_1.0.0rc1.json"
CAPTURE = json_util.loads(STORE.read_text())
SERVICE, GROUP_SERVICE = CAPTURE["services"]
NAMESPACED_USER = "https://login.other.example.org/|dave"
# Rule of 1.0.0rc1 in stored form, and the path of the rule that replaces it
RC1_RULE = {"ptype": "p", "v0": "owner", "v1": USERS_RULE_RC1[2]}
USERS_RULE_PATH = f"{BASE}/services/:id/permissions/users/*"
# Token of the invitation in the tests (cf. `stored()`)
INVITATION = "rc1"


@pytest.fixture
def stored(
    db_client: MongoClient[dict[str, Any]], app_env: None
) -> MongoClient[dict[str, Any]]:
    """Store the data of 1.0.0rc1, with its invitation still open.

    Returns:
        Client of the MongoDB with the data.
    """
    for name, documents in CAPTURE["store"].items():
        db_client[DB][name].insert_many(deepcopy(documents))
    db_client[DB]["invitations"].update_many(
        {},
        {
            "$set": {
                "token_hash": hash_token(INVITATION),
                "expires_at": datetime.now(UTC) + timedelta(days=1),
            }
        },
    )
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
