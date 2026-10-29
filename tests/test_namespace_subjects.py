"""Tests for the migration to namespaced subjects (several token issuers).

See `cloud_registry/namespace_subjects.py`; `tests/test_issuers.py` tests an
upgrade with it end to end.
"""

from pathlib import Path
from typing import Any

from flask.testing import FlaskClient
from foca.security.access_control.constants import CONFIGURED_FIELD
import pytest

from cloud_registry import namespace_subjects as namespace_module
from cloud_registry.ga4gh.registry.invitations import (
    ACCEPTED,
    CREATED_AS,
    TOKEN_HASH,
)
from cloud_registry.ga4gh.registry.permissions import INVITATIONS
from cloud_registry.ga4gh.registry.service import CREATED_BY as SERVICE_CREATOR
from cloud_registry.ga4gh.registry.service import KEY
from cloud_registry.ga4gh.registry.service_limits import COUNT, SERVICE_COUNTS
from cloud_registry.ga4gh.registry.service_types import CREATED_BY
from cloud_registry.namespace_subjects import (
    _policy_collection,
    check_namespace,
    main,
    namespace_subjects,
)
from cloud_registry.utils import get_collection
from tests.conftest import ISSUER, ISSUER2
from tests.test_access_control import _create_app

# Overlay trusting both issuers, with namespaced subjects (the default)
NAMESPACED: dict[str, Any] = {"security": {"auth": {"issuers": [ISSUER, ISSUER2]}}}
BARE = "alice"
NAMESPACED_ALICE = f"{ISSUER}|{BARE}"
OTHER = f"{ISSUER2}|bob"


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> FlaskClient:
    """Test client of the app trusting both issuers, with stored subjects.

    Stored are: rules naming the bare user `alice` (a resource role, a role,
    and a configured rule), a rule of a namespaced user, service types added
    by `alice` and by a namespaced user, services registered by `alice`, by
    a namespaced user and by nobody (with keys, as if valid), with counters
    of `alice` and of the namespaced `alice` (who registered one since the
    config change), display names of `alice` (one for a service of which
    the namespaced user already has a name), and invitations created as or
    accepted by `alice` (as owner, through a group, or by a namespaced user
    or an admin).
    """
    client = _create_app(monkeypatch, tmp_path, NAMESPACED)
    with client.application.app_context():
        _policy_collection().insert_many(
            [
                {"ptype": "g2", "v0": f"user:{BARE}", "v1": "owner", "v2": "/s/1"},
                {"ptype": "g", "v0": f"user:{BARE}", "v1": "auditor"},
                {
                    "ptype": "p",
                    "v0": f"user:{BARE}",
                    "v1": "/x",
                    "v2": "GET",
                    CONFIGURED_FIELD: True,
                },
                {"ptype": "g2", "v0": f"user:{OTHER}", "v1": "owner", "v2": "/s/2"},
            ]
        )
        get_collection("service_types").insert_many(
            [
                {"group": "a", "artifact": "b", "version": "1", CREATED_BY: BARE},
                {"group": "a", "artifact": "c", "version": "1", CREATED_BY: OTHER},
                {"group": "a", "artifact": "d", "version": "1", CREATED_BY: None},
            ]
        )
        get_collection("services").insert_many(
            [
                {"id": "S1", KEY: "S1", SERVICE_CREATOR: BARE},
                {"id": "S2", KEY: "S2", SERVICE_CREATOR: BARE},
                {"id": "S3", KEY: "S3", SERVICE_CREATOR: OTHER},
                {"id": "S4", KEY: "S4", SERVICE_CREATOR: None},
                {"id": "S5", KEY: "S5", SERVICE_CREATOR: NAMESPACED_ALICE},
            ]
        )
        get_collection(SERVICE_COUNTS).insert_many(
            [
                {"_id": BARE, COUNT: 2},
                {"_id": NAMESPACED_ALICE, COUNT: 1},
                {"_id": OTHER, COUNT: 1},
            ]
        )
        get_collection("user_names").insert_many(
            [
                {"service_id": "S1", "user_id": BARE, "name": "Alice"},
                {"service_id": "S2", "user_id": BARE, "name": "Alice (old)"},
                {"service_id": "S2", "user_id": NAMESPACED_ALICE, "name": "Alice"},
            ]
        )
        # unique hashes of tokens (indexed)
        get_collection(INVITATIONS).insert_many(
            {**invitation, TOKEN_HASH: invitation["_id"]}
            for invitation in [
                {"_id": "I1", "created_by": BARE, CREATED_AS: {"user": BARE}},
                {
                    "_id": "I2",
                    "created_by": OTHER,
                    CREATED_AS: {"group": "team"},
                    ACCEPTED: {"user_id": BARE, "name": "Alice"},
                },
                {
                    "_id": "I3",
                    "created_by": NAMESPACED_ALICE,
                    CREATED_AS: {"admin": True},
                    ACCEPTED: {"user_id": OTHER},
                },
                {"_id": "I4", "created_by": BARE, CREATED_AS: {"group": "team"}},
            ]
        )
    return client


def _rules() -> list[tuple[Any, ...]]:
    """Get the stored policy rules, without identifiers.

    Must be called in an app context.

    Returns:
        Values of the rules (`ptype`, `v0`, ...), sorted.
    """
    return sorted(
        tuple(rule.get(key) for key in ("ptype", "v0", "v1", "v2"))
        for rule in _policy_collection().find({"v1": {"$ne": None}})
        if str(rule.get("v0", "")).startswith("user:")
    )


def _invitations() -> dict[str, tuple[Any, ...]]:
    """Get the users of the stored invitations.

    Must be called in an app context.

    Returns:
        Creator, how they created it, and who accepted it (if accepted), by
        invitation identifier.
    """
    return {
        invitation["_id"]: (
            invitation["created_by"],
            invitation[CREATED_AS],
            invitation.get(ACCEPTED, {}).get("user_id"),
        )
        for invitation in get_collection(INVITATIONS).find()
    }


def test_namespace_subjects(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Bare subjects are namespaced, others are kept; runs can be repeated.

    Configured rules are kept, with a warning.
    """
    with client.application.app_context():
        counts = namespace_subjects(ISSUER)
        assert counts == {
            "rules": 2,
            "configured_rules": 1,
            "service_types": 1,
            "services": 2,
            "user_names": 2,
            "invitations": 3,
        }
        assert "configured policy rules" in caplog.text
        assert _rules() == [
            ("g", f"user:{NAMESPACED_ALICE}", "auditor", None),
            ("g2", f"user:{NAMESPACED_ALICE}", "owner", "/s/1"),
            ("g2", f"user:{OTHER}", "owner", "/s/2"),
            ("p", f"user:{BARE}", "/x", "GET"),
        ]
        creators = {
            service_type["artifact"]: service_type[CREATED_BY]
            for service_type in get_collection("service_types").find()
        }
        assert creators == {"b": NAMESPACED_ALICE, "c": OTHER, "d": None}
        services = {
            service["id"]: service[SERVICE_CREATOR]
            for service in get_collection("services").find()
        }
        assert services == {
            "S1": NAMESPACED_ALICE,
            "S2": NAMESPACED_ALICE,
            "S3": OTHER,
            "S4": None,
            "S5": NAMESPACED_ALICE,
        }
        counters = {
            counter["_id"]: counter[COUNT]
            for counter in get_collection(SERVICE_COUNTS).find()
        }
        assert counters == {NAMESPACED_ALICE: 3, OTHER: 1}
        names = sorted(
            (name["service_id"], name["user_id"], name["name"])
            for name in get_collection("user_names").find()
        )
        # the namespaced user's name is kept
        assert names == [
            ("S1", NAMESPACED_ALICE, "Alice"),
            ("S2", NAMESPACED_ALICE, "Alice"),
        ]
        assert _invitations() == {
            "I1": (NAMESPACED_ALICE, {"user": NAMESPACED_ALICE}, None),
            "I2": (OTHER, {"group": "team"}, NAMESPACED_ALICE),
            "I3": (NAMESPACED_ALICE, {"admin": True}, OTHER),
            "I4": (NAMESPACED_ALICE, {"group": "team"}, None),
        }
        counts = namespace_subjects(ISSUER)
        assert counts == {
            "rules": 0,
            "configured_rules": 1,
            "service_types": 0,
            "services": 0,
            "user_names": 0,
            "invitations": 0,
        }


def test_subjects_with_separator(client: FlaskClient) -> None:
    """Bare subjects with the separator of namespaces are namespaced.

    E.g., `auth0|123`; only subjects that start with a namespace of the
    config and the separator are taken as namespaced.
    """
    with client.application.app_context():
        get_collection("service_types").insert_one(
            {"group": "a", "artifact": "e", "version": "1", CREATED_BY: "auth0|1"}
        )
        _policy_collection().insert_one(
            {"ptype": "g2", "v0": "user:auth0|1", "v1": "owner", "v2": "/s/3"}
        )
        namespace_subjects(ISSUER)
        stored = get_collection("service_types").find_one({"artifact": "e"})
        assert stored is not None
        assert stored[CREATED_BY] == f"{ISSUER}|auth0|1"
        assert ("g2", f"user:{ISSUER}|auth0|1", "owner", "/s/3") in _rules()


def test_invitation_changed_meanwhile(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invitations that change while they are rewritten are not counted.

    They are left as they are (here: deleted, e.g., as revoked), so that,
    e.g., released acceptances aren't recreated, and another run rewrites
    them.
    """
    get_collection_of_app = namespace_module.get_collection

    class ChangingCollection:
        """Invitations, of which one is deleted after they are read."""

        def __init__(self, collection: Any) -> None:
            """Wrap the collection.

            Args:
                collection: Collection of the invitations.
            """
            self.collection = collection

        def find(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            """Read the invitations, then delete the first one.

            Args:
                *args: Positional arguments of `Collection.find()`.
                **kwargs: Keyword arguments of `Collection.find()`.

            Returns:
                The invitations as read.
            """
            invitations = list(self.collection.find(*args, **kwargs))
            self.collection.delete_one({"_id": "I1"})
            return invitations

        def update_one(self, *args: Any, **kwargs: Any) -> Any:
            """Update an invitation.

            Args:
                *args: Positional arguments of `Collection.update_one()`.
                **kwargs: Keyword arguments of `Collection.update_one()`.

            Returns:
                The result of the update.
            """
            return self.collection.update_one(*args, **kwargs)

    def get_collection_changing(name: str) -> Any:
        """Get a collection; that of invitations changes after reading.

        Args:
            name: Name of the collection.

        Returns:
            The collection.
        """
        collection = get_collection_of_app(name)
        return ChangingCollection(collection) if name == INVITATIONS else collection

    monkeypatch.setattr(namespace_module, "get_collection", get_collection_changing)
    with client.application.app_context():
        assert namespace_subjects(ISSUER)["invitations"] == 2
        assert "I1" not in _invitations()


def test_dry_run(client: FlaskClient) -> None:
    """A dry run counts the subjects, without changes."""
    with client.application.app_context():
        before = _rules()
        counts = namespace_subjects(ISSUER, dry_run=True)
        assert counts == {
            "rules": 2,
            "configured_rules": 1,
            "service_types": 1,
            "services": 2,
            "user_names": 2,
            "invitations": 3,
        }
        assert _rules() == before
        assert get_collection("user_names").count_documents({"user_id": BARE}) == 2
        assert get_collection("services").count_documents({SERVICE_CREATOR: BARE}) == 2
        assert get_collection(SERVICE_COUNTS).count_documents({"_id": BARE}) == 1
        assert _invitations()["I1"] == (BARE, {"user": BARE}, None)


@pytest.mark.parametrize(
    ("namespace", "match"),
    [
        ("https://other.example.org/", "No trusted issuer"),
        ("", "must not be empty"),
        ("a|b", "namespace"),
        ("a,b", "namespace"),
        ("a\nb", "namespace"),
    ],
)
def test_check_namespace_invalid(
    client: FlaskClient, namespace: str, match: str
) -> None:
    """Namespaces must be valid, and those of trusted issuers."""
    with client.application.app_context(), pytest.raises(ValueError, match=match):
        check_namespace(namespace)


def test_check_namespace_bare_issuer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Bare subjects of an issuer with the namespace '' are not namespaced.

    They are that issuer's, so they must not be taken for another issuer's.
    """
    overlay = {
        "security": {
            "auth": {"issuers": [{"issuer": ISSUER, "namespace": ""}, ISSUER2]}
        }
    }
    client = _create_app(monkeypatch, tmp_path, overlay)
    with client.application.app_context(), pytest.raises(ValueError, match="bare"):
        check_namespace(ISSUER2)


def test_partial_run(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running instances reload the rules, also if a run fails."""

    def fail(namespace: str, dry_run: bool) -> tuple[int, int]:
        """Fail namespacing the rules.

        Args:
            namespace: Namespace of the issuer's subjects.
            dry_run: Only count the subjects.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("interrupted")

    monkeypatch.setattr("cloud_registry.namespace_subjects._namespace_rules", fail)
    with client.application.app_context():
        adapter = client.application.config["casbin_adapter"]
        calls: list[None] = []
        monkeypatch.setattr(adapter, "mark_changed", lambda: calls.append(None))
        with pytest.raises(RuntimeError, match="interrupted"):
            namespace_subjects(ISSUER)
        assert calls == [None]


def test_check_namespace_bare(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """With a single issuer, subjects are bare: there is nothing to namespace."""
    client = _create_app(monkeypatch, tmp_path)
    with client.application.app_context(), pytest.raises(ValueError, match="first"):
        check_namespace(ISSUER)


def test_main(client: FlaskClient, capsys: pytest.CaptureFixture[str]) -> None:
    """The command line runs the migration with the app's config."""
    assert main([ISSUER, "--dry-run"]) == 0
    assert "To namespace rules: 2" in capsys.readouterr().out
    assert main([ISSUER]) == 0
    assert "Namespaced rules: 2" in capsys.readouterr().out
    assert main(["https://other.example.org/"]) == 1
    assert "error: No trusted issuer" in capsys.readouterr().err
