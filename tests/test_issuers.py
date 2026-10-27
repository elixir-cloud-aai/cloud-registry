"""Tests for deployments that trust several token issuers.

With several trusted issuers, FOCA namespaces subjects by issuer
(`<namespace>|<sub>`; by default, the namespace is the issuer), so that equal
subjects of different issuers are different users. The tests use the issuers
of the mocked identity providers (`ISSUER`, `ISSUER2`, see
`tests/conftest.py`).
"""

from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask.testing import FlaskClient
import pytest

from cloud_registry.namespace_subjects import namespace_subjects
from tests.conftest import ADMIN, ISSUER, ISSUER2, USER, make_headers
from tests.test_access_control import BASE, _create_app, _service, register

ALICE = USER
# Alice of the second issuer: another user, with the same `sub` claim
ALICE2 = f"{ISSUER2}|{ALICE}"
# Overlay trusting both issuers; the first one keeps its subjects bare
# (`namespace: ""`), so that subjects stored before stay valid
TWO_ISSUERS: dict[str, Any] = {
    "security": {"auth": {"issuers": [{"issuer": ISSUER, "namespace": ""}, ISSUER2]}}
}


def alice2() -> dict[str, str]:
    """Get request headers with an access token of Alice of `ISSUER2`.

    Returns:
        Request headers.
    """
    return make_headers(ALICE, issuer=ISSUER2)


def _update(client: FlaskClient, service_id: str, headers: dict[str, str]) -> int:
    """Replace a service with the test service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        headers: Request headers, with the access token of the user.

    Returns:
        Status code of the response.
    """
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=headers
    )
    return response.status_code


@pytest.fixture
def two_issuers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> FlaskClient:
    """Test client of the app trusting both issuers."""
    return _create_app(monkeypatch, tmp_path, TWO_ISSUERS)


def test_equal_subjects_different_users(two_issuers: FlaskClient) -> None:
    """Equal subjects of different issuers are different users."""
    client = two_issuers
    service_id = register(client, ALICE)
    assert _update(client, service_id, alice2()) == 403
    response = client.get(f"{BASE}/me", headers=alice2())
    assert response.status_code == 200
    me = response.get_json()
    assert (me["user_id"], me["services"]) == (ALICE2, [])


@pytest.mark.parametrize("encode", [False, True])
def test_namespaced_permissions(two_issuers: FlaskClient, encode: bool) -> None:
    """Namespaced users are granted and revoked roles via the API.

    Their identifiers contain slashes, encoded or not.
    """
    client = two_issuers
    service_id = register(client, ALICE)
    user = quote(ALICE2, safe="") if encode else ALICE2
    url = f"{BASE}/services/{service_id}/permissions/{user}"
    owner = make_headers(ALICE)
    response = client.put(url, json={"role": "maintainer"}, headers=owner)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"user_id": ALICE2, "role": "maintainer"}
    assert _update(client, service_id, alice2()) == 200
    listed = client.get(f"{BASE}/services/{service_id}/permissions", headers=owner)
    assert {"user_id": ALICE2, "role": "maintainer"} in listed.get_json()
    assert client.delete(url, headers=owner).status_code == 200
    assert _update(client, service_id, alice2()) == 403


def test_group_permissions_routed(two_issuers: FlaskClient) -> None:
    """Paths of roles of groups are not taken for users with slashes."""
    client = two_issuers
    service_id = register(client, ALICE)
    response = client.put(
        f"{BASE}/services/{service_id}/permissions/groups/team/a",
        json={"role": "maintainer"},
        headers=make_headers(ADMIN),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == {"group": "team/a", "role": "maintainer"}


def test_service_types_by_issuer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Limits of unused service types are per user, also across issuers."""
    limit = {"custom": {"access_control": {"max_unused_service_types": 1}}}
    client = _create_app(monkeypatch, tmp_path, TWO_ISSUERS, limit)

    def add(artifact: str, headers: dict[str, str]) -> int:
        """Add a service type.

        Args:
            artifact: Artifact of the type.
            headers: Request headers, with the access token of the user.

        Returns:
            Status code of the response.
        """
        service_type = {"group": "org.example", "artifact": artifact, "version": "1"}
        response = client.post(
            f"{BASE}/services/types", json=service_type, headers=headers
        )
        return response.status_code

    assert add("first", make_headers(ALICE)) == 201
    assert add("second", alice2()) == 201
    assert add("third", make_headers(ALICE)) == 409
    assert add("third", alice2()) == 409


def test_upgrade_bare_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Adding an issuer keeps stored users of the first one (`namespace: ""`)."""
    client = _create_app(monkeypatch, tmp_path)
    service_id = register(client, ALICE)
    client = _create_app(monkeypatch, tmp_path, TWO_ISSUERS)
    assert _update(client, service_id, make_headers(ALICE)) == 200
    assert _update(client, service_id, alice2()) == 403


def test_upgrade_namespaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Adding an issuer with namespaced subjects needs the migration script.

    Without it, stored users of the first issuer lose their roles; with it
    (`scripts/namespace_subjects.py`), they keep them, also their roles on
    services and the service types they added.
    """
    client = _create_app(monkeypatch, tmp_path)
    service_id = register(client, ALICE)
    namespaced = {"security": {"auth": {"issuers": [ISSUER, ISSUER2]}}}
    client = _create_app(monkeypatch, tmp_path, namespaced)
    assert _update(client, service_id, make_headers(ALICE)) == 403
    with client.application.app_context():
        counts = namespace_subjects(ISSUER)
    assert counts["rules"] == 1
    # running instances reload the rules (`mark_changed()`)
    assert _update(client, service_id, make_headers(ALICE)) == 200
    me = client.get(f"{BASE}/me", headers=make_headers(ALICE)).get_json()
    assert me["user_id"] == f"{ISSUER}|{ALICE}"
    assert {"service_id": service_id, "role": "owner"} in me["services"]
    assert _update(client, service_id, alice2()) == 403
