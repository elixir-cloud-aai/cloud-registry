"""Tests for the migration to namespaced subjects (several token issuers).

See `cloud_registry/namespace_subjects.py`; `tests/test_issuers.py` tests an
upgrade with it end to end.
"""

from pathlib import Path
from typing import Any

from flask.testing import FlaskClient
from foca.security.access_control.constants import CONFIGURED_FIELD
import pytest

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
    by `alice` and by a namespaced user, and display names of `alice` (one
    for a service of which the namespaced user already has a name).
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
        get_collection("user_names").insert_many(
            [
                {"service_id": "S1", "user_id": BARE, "name": "Alice"},
                {"service_id": "S2", "user_id": BARE, "name": "Alice (old)"},
                {"service_id": "S2", "user_id": NAMESPACED_ALICE, "name": "Alice"},
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
            "user_names": 2,
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
        names = sorted(
            (name["service_id"], name["user_id"], name["name"])
            for name in get_collection("user_names").find()
        )
        # the namespaced user's name is kept
        assert names == [
            ("S1", NAMESPACED_ALICE, "Alice"),
            ("S2", NAMESPACED_ALICE, "Alice"),
        ]
        counts = namespace_subjects(ISSUER)
        assert counts == {
            "rules": 0,
            "configured_rules": 1,
            "service_types": 0,
            "user_names": 0,
        }


def test_dry_run(client: FlaskClient) -> None:
    """A dry run counts the subjects, without changes."""
    with client.application.app_context():
        before = _rules()
        counts = namespace_subjects(ISSUER, dry_run=True)
        assert counts == {
            "rules": 2,
            "configured_rules": 1,
            "service_types": 1,
            "user_names": 2,
        }
        assert _rules() == before
        assert get_collection("user_names").count_documents({"user_id": BARE}) == 2


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
        with pytest.raises(RuntimeError):
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
