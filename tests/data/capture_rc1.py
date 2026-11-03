r"""Make `store_1.0.0rc1.json`, the data that 1.0.0rc1 stores in MongoDB.

This runs in the tests of 1.0.0rc1, through its API, on a real MongoDB without
the database `serviceStore` (e.g., an empty one on port 27017). It isn't a test
of this version, whose tests only read its result (`tests/test_upgrade_rc1.py`).
From the root of the repository, with the version of MongoDB of `docker-compose.yaml`:

    git worktree add ../rc1 v1.0.0rc1
    cp tests/data/capture_rc1.py ../rc1/tests/test_capture_rc1.py
    cd ../rc1
    uv sync --locked --extra dev
    MONGODB_TEST_URI=mongodb://localhost:27017 \
        CAPTURE_FILE="$OLDPWD/tests/data/store_1.0.0rc1.json" \
        uv run python -m pytest tests/test_capture_rc1.py

The file in the repository was made so, on `v1.0.0rc1` and MongoDB 7.0.34; all its
identifiers and times are new on each run.

Stored are the service of alice, with roles of users (bob, carol and a namespaced
user) and of a group (maintainer), and three invitations:

- an open one, for the role `maintainer`;
- an expired one, for the role `owner`, whose time is set in the database, as
  1.0.0rc1 serves no such invitation, and its TTL index removes them within a
  minute;
- one that grace accepted, with her name and verified email address.

Also stored is the service whose only owner is the group. The hashes of tokens are
replaced, so that no such value is in the repository.
"""

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bson import json_util
from bson.json_util import RELAXED_JSON_OPTIONS
from flask.testing import FlaskClient
from pymongo import MongoClient
import pytest

from cloud_registry.ga4gh.registry import invitations as invitations_module
from tests.conftest import TEAM_GROUP
from tests.mock_data import DB
from tests.test_access_control import (
    ALICE,
    BASE,
    BOB,
    CAROL,
    grant,
    grant_group,
    headers,
    register,
    revoke,
)
from tests.test_invitations import accept, token_of

# Collections with the data of the registry; FOCA's log of changed rules is left out
COLLECTIONS = (
    "invitations",
    "policy_rules",
    "service_counts",
    "service_types",
    "services",
    "user_names",
)
GRACE = "grace@example.org"
NAMESPACED_USER = "https://login.other.example.org/|dave"


def test_capture(
    monkeypatch: pytest.MonkeyPatch,
    client: FlaskClient,
    db_client: MongoClient[dict[str, Any]],
) -> None:
    """Store the data through the API, and write it to `CAPTURE_FILE`."""
    service = register(client)
    assert grant(client, service, BOB, "maintainer") == 200
    assert grant(client, service, CAROL, "owner") == 200
    url = f"{BASE}/services/{service}/permissions/{quote(NAMESPACED_USER, safe='')}"
    response = client.put(url, json={"role": "maintainer"}, headers=headers(ALICE))
    assert response.status_code == 200
    assert grant_group(client, service, TEAM_GROUP, "maintainer") == 200
    # stores the name of bob
    assert client.get(f"{BASE}/me", headers=headers(BOB)).status_code == 200

    group_service = register(client)
    assert grant_group(client, group_service, TEAM_GROUP, "owner") == 200
    assert revoke(client, group_service, ALICE) == 200

    token_of(client, service)
    token_of(client, service, "owner")
    accepted = token_of(client, service)
    monkeypatch.setattr(
        invitations_module,
        "get_current_claims",
        lambda: {"name": "Grace", "email": GRACE, "email_verified": True},
    )
    assert accept(client, accepted, user=GRACE).status_code == 200

    database = db_client[DB]
    database["invitations"].update_one(
        {"role": "owner"},
        {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    store = {name: list(database[name].find()) for name in COLLECTIONS}
    assert len(store["invitations"]) == 3
    for invitation in store["invitations"]:
        # no credential: what the tests replace
        invitation["token_hash"] = "replaced-by-the-tests"  # noqa: S105
    capture = {"services": [service, group_service], "store": store}
    Path(os.environ["CAPTURE_FILE"]).write_text(
        json_util.dumps(
            capture, indent=1, sort_keys=True, json_options=RELAXED_JSON_OPTIONS
        )
        + "\n"
    )
