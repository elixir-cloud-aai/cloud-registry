"""Tests for the codes of conflicts (`409`), with the app created from its config.

Each conflict that the registry returns has a code (`code`), documented in
the API specification (schema `ConflictError`).
"""

from pathlib import Path
import re
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca.security.access_control import LastOwner
import pytest
from werkzeug.test import TestResponse
import yaml

from cloud_registry import exceptions
from cloud_registry.exceptions import (
    CONFLICT_CODE,
    CONFLICT_DETAIL,
    CodedConflict,
    Conflict,
    problem,
)
from cloud_registry.utils import get_custom_config
from tests.conftest import ADMIN, APP_DIR, USER, make_headers

BASE = "/ga4gh/registry/v1"
TES = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": TES,
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}


def _code(response: TestResponse) -> str:
    """Get the code of a conflict.

    Args:
        response: Response, a conflict.

    Returns:
        `code` of the problem details.
    """
    assert response.status_code == 409, response.get_json()
    code: str = response.get_json()["code"]
    return code


def _register(client: FlaskClient, user: str = ADMIN, **kwargs: Any) -> TestResponse:
    """Register the test service (`POST /services`).

    Args:
        client: Test client of the app.
        user: User who registers it.
        kwargs: Fields to set.

    Returns:
        Response.
    """
    return client.post(
        f"{BASE}/services", json={**SERVICE, **kwargs}, headers=make_headers(user)
    )


def _set_access_control(app: App, **kwargs: Any) -> None:
    """Set options of `custom.access_control`.

    Args:
        app: The app.
        kwargs: Options and their values.
    """
    with app.app.app_context():
        config = get_custom_config().access_control
        for key, value in kwargs.items():
            setattr(config, key, value)


def test_duplicate_service(client: FlaskClient) -> None:
    """Duplicates have the code `duplicate-service`, also on updates."""
    service_id = _register(client).get_json()
    response = _register(client)
    assert _code(response) == "duplicate-service"
    assert response.get_json()["serviceId"] == service_id
    other_id = _register(client, url="https://other.example.org").get_json()
    response = client.put(
        f"{BASE}/services/{other_id}", json=SERVICE, headers=make_headers(ADMIN)
    )
    assert _code(response) == "duplicate-service"


def test_pending_service(app: App, client: FlaskClient) -> None:
    """Updates and verifications of pending services: `pending-service`."""
    _set_access_control(app, service_types="moderated")
    service_id = _register(client, user=USER).get_json()
    response = client.put(
        f"{BASE}/services/{service_id}",
        json={**SERVICE, "name": "New"},
        headers=make_headers(USER),
    )
    assert _code(response) == "pending-service"
    response = client.put(
        f"{BASE}/services/{service_id}/verification", headers=make_headers(ADMIN)
    )
    assert _code(response) == "pending-service"


def test_unused_types_limit(app: App, client: FlaskClient) -> None:
    """New types beyond the limit: `unused-types-limit`."""
    _set_access_control(app, max_unused_service_types=0)
    response = client.post(
        f"{BASE}/services/types", json=TES, headers=make_headers(USER)
    )
    assert _code(response) == "unused-types-limit"
    assert _code(_register(client, user=USER)) == "unused-types-limit"
    _set_access_control(app, max_unused_service_types=10)
    service_id = _register(client, user=USER).get_json()
    _set_access_control(app, max_unused_service_types=0)
    new_type = {**TES, "version": "2.0.0"}
    response = client.put(
        f"{BASE}/services/{service_id}",
        json={**SERVICE, "type": new_type},
        headers=make_headers(USER),
    )
    assert _code(response) == "unused-types-limit"


def test_service_type_registered(client: FlaskClient) -> None:
    """Registered types: `service-type-registered`."""
    path = f"{BASE}/services/types"
    assert client.post(path, json=TES, headers=make_headers()).status_code == 201
    response = client.post(path, json=TES, headers=make_headers())
    assert _code(response) == "service-type-registered"


def test_service_type_in_use(client: FlaskClient) -> None:
    """Removals of types that services have: `service-type-in-use`."""
    _register(client)
    response = client.delete(
        f"{BASE}/services/types", query_string=TES, headers=make_headers(ADMIN)
    )
    assert _code(response) == "service-type-in-use"


def test_verification_mismatch(client: FlaskClient) -> None:
    """Verifications of other URLs: `verification-mismatch`."""
    service_id = _register(client).get_json()
    response = client.put(
        f"{BASE}/services/{service_id}/verification",
        json={"url": "https://other.example.org", "type": TES},
        headers=make_headers(ADMIN),
    )
    assert _code(response) == "verification-mismatch"


def test_last_owner(client: FlaskClient) -> None:
    """Removals of the last owner: `last-owner`."""
    service_id = _register(client, user=USER).get_json()
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/{USER}", headers=make_headers()
    )
    assert _code(response) == "last-owner"
    assert response.get_json()["detail"] == CONFLICT_DETAIL


def test_configured_policy(client: FlaskClient) -> None:
    """Changes of configured rules via FOCA's permissions API: `conflict`."""
    path = "/admin/access-control/permissions"
    rules = client.get(path, headers=make_headers(ADMIN)).get_json()
    rule_id = rules["results"][0]["id"]
    response = client.delete(f"{path}/{rule_id}", headers=make_headers(ADMIN))
    assert _code(response) == CONFLICT_CODE


def test_problem_conflict(app: App) -> None:
    """Conflicts without a code of their own have the code `conflict`.

    Extension members are kept, except for one named `code`.
    """
    with app.app.app_context():
        response = problem(Conflict("Other conflict."), serviceId="A", code="x")
    assert response.get_json() == {
        "title": "Conflict",
        "status": 409,
        "detail": "Other conflict.",
        "code": CONFLICT_CODE,
        "serviceId": "A",
    }


def test_problem_mapped_conflict(app: App) -> None:
    """Conflicts with an entry in the mapping have its code."""
    with app.app.app_context():
        response = problem(LastOwner("Last owner."))
    assert response.get_json()["code"] == "last-owner"


def _subclasses(cls: type[CodedConflict]) -> set[type[CodedConflict]]:
    """Get the subclasses of a class, recursively.

    Args:
        cls: Class.

    Returns:
        Its subclasses, and theirs.
    """
    return {
        sub for direct in cls.__subclasses__() for sub in {direct, *_subclasses(direct)}
    }


@pytest.fixture
def documented_codes() -> list[str]:
    """Get the codes of conflicts documented in the API specification.

    Returns:
        The values of `code` in the schema `ConflictError`.
    """
    spec = yaml.safe_load(Path(APP_DIR, "api", "additions.openapi.yaml").read_text())
    schema = spec["components"]["schemas"]["ConflictError"]["allOf"][1]
    codes: list[str] = schema["properties"]["code"]["x-extensible-enum"]
    return codes


def test_codes_documented(app: App, documented_codes: list[str]) -> None:
    """All codes are documented in the API specification and docs, only those."""
    import cloud_registry.ga4gh.registry.server  # noqa: F401, PLC0415 (subclasses)

    codes = {cls.problem_code for cls in _subclasses(CodedConflict)}
    codes |= {
        str(details["code"])
        for details in exceptions.exceptions.values()
        if "code" in details
    }
    assert codes == set(documented_codes)
    docs = Path(APP_DIR, "..", "docs", "api.md").read_text()
    table = docs.split("| Code | Conflict |", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"^\| `([a-z-]+)` \|", table, re.MULTILINE)) == codes
