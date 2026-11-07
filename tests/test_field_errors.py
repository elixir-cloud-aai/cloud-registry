"""Tests of the invalid fields of write requests (`errors` of `400` responses).

The codes of the app's own checks (`cloud_registry.exceptions`) are tested
here for registrations, and with their other operations in the modules of
these (`tests/test_service_import.py`, `tests/test_rename.py`,
`tests/test_verification.py`); FOCA's codes of its schema validation in
`tests/test_app.py`. The specification and the docs document all codes.
"""

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca import get_custom_config
import pytest
from werkzeug.test import TestResponse
import yaml

from cloud_registry.exceptions import FIELD_ERROR_CODES
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection
from tests.conftest import ADMIN, APP_DIR, invalid_fields, make_headers

BASE = "/ga4gh/registry/v1"
# Valid as per the API specification
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
DOCS = Path(__file__).parents[1] / "docs" / "api.md"
SPEC = APP_DIR / "api" / "additions.openapi.yaml"
# Registers a service, returning the response
Write = Callable[[dict[str, Any]], TestResponse]


def _service(**kwargs: Any) -> dict[str, Any]:
    """Get a copy of `SERVICE`, with fields set or replaced.

    Args:
        kwargs: Fields to set.

    Returns:
        Service.
    """
    service = deepcopy(SERVICE)
    service.update(kwargs)
    return service


@pytest.fixture(params=[("post", ""), ("put", "/abc")], ids=["post", "put"])
def write(request: pytest.FixtureRequest, client: FlaskClient) -> Write:
    """Register services as admin, with `POST`, or `PUT` with a new identifier.

    Returns:
        Function taking the service and returning the response.
    """
    method, path = request.param

    def _write(service: dict[str, Any]) -> TestResponse:
        response: TestResponse = getattr(client, method)(
            f"{BASE}/services{path}", json=service, headers=make_headers(ADMIN)
        )
        return response

    return _write


@pytest.mark.parametrize(
    "service, expected",
    [
        (
            _service(url="https://user:secret@tes.example.org"),
            ("#/url", "invalid-url"),
        ),
        (_service(url="http://secret.example.org"), ("#/url", "scheme-not-allowed")),
        (_service(contactUrl="mailto:secret"), ("#/contactUrl", "invalid-url")),
        (
            _service(documentationUrl="ftp://secret.example.org"),
            ("#/documentationUrl", "scheme-not-allowed"),
        ),
        (
            _service(organization={"name": "Org", "url": "https://sec<ret.org"}),
            ("#/organization/url", "invalid-url"),
        ),
        (_service(environment="secret"), ("#/environment", "invalid-value")),
    ],
    ids=[
        "url",
        "url-scheme",
        "contact-url",
        "link-scheme",
        "organization-url",
        "environment",
    ],
)
def test_registration_field(
    client: FlaskClient,
    write: Write,
    service: dict[str, Any],
    expected: tuple[str, str],
) -> None:
    """Invalid fields of registrations are reported, without their values."""
    assert invalid_fields(write(service), "secret") == [expected]
    assert client.get(f"{BASE}/services").get_json() == []


def test_registration_all_fields(write: Write) -> None:
    """All invalid fields of a registration are reported together."""
    service = _service(
        url="http://tes.example.org",
        contactUrl="https://user@example.org",
        documentationUrl="javascript:alert(1)",
        environment="production",
    )
    response = write(service)
    assert invalid_fields(response) == [
        ("#/url", "scheme-not-allowed"),
        ("#/contactUrl", "invalid-url"),
        ("#/documentationUrl", "scheme-not-allowed"),
        ("#/environment", "invalid-value"),
    ]
    assert response.get_json()["detail"] == (
        "Invalid fields: #/url (scheme-not-allowed); #/contactUrl (invalid-url); "
        "#/documentationUrl (scheme-not-allowed); #/environment (invalid-value)"
    )


def test_registration_messages(write: Write) -> None:
    """Messages of invalid fields say why, without the values."""
    service = _service(
        url="https://user:secret@tes.example.org",
        environment="production",
    )
    response = write(service)
    invalid_fields(response, "secret", "production")
    assert [error["detail"] for error in response.get_json()["errors"]] == [
        "Must not contain user info.",
        "Must be one of: 'prod', 'test', 'dev', 'staging'.",
    ]


def test_unregistered_type(app: App, client: FlaskClient) -> None:
    """Types that users may not add are reported with the other fields.

    In mode `controlled`, only admins add types; the type is not quoted.
    """
    with app.app.app_context():
        get_custom_config(CustomConfig).access_control.service_types = "controlled"
    new_type = {"group": "org.secret", "artifact": "tes", "version": "1"}
    response = client.post(
        f"{BASE}/services",
        json=_service(type=new_type, url="http://tes.example.org"),
        headers=make_headers(),
    )
    assert invalid_fields(response, "org.secret") == [
        ("#/url", "scheme-not-allowed"),
        ("#/type", "unregistered-type"),
    ]
    response = client.post(
        f"{BASE}/services", json=_service(type=new_type), headers=make_headers()
    )
    assert invalid_fields(response, "org.secret") == [("#/type", "unregistered-type")]
    assert client.get(f"{BASE}/services").get_json() == []


def test_unusable_service_id(client: FlaskClient) -> None:
    """Identifiers of `PUT` that can't be stored in roles are reported.

    As the parameter `serviceId`; the service is not registered.
    """
    response = client.put(
        f"{BASE}/services/a,b", json=_service(), headers=make_headers(ADMIN)
    )
    assert invalid_fields(response) == [("serviceId", "unusable-id")]
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services/a,b").status_code == 404


def test_unusable_service_id_with_fields(client: FlaskClient) -> None:
    """Unusable identifiers of `PUT` are reported with the body's fields."""
    response = client.put(
        f"{BASE}/services/a,b",
        json=_service(url="http://tes.example.org"),
        headers=make_headers(ADMIN),
    )
    assert invalid_fields(response) == [
        ("#/url", "scheme-not-allowed"),
        ("serviceId", "unusable-id"),
    ]


def test_stored_unusable_service_id_updated(app: App, client: FlaskClient) -> None:
    """Stored services keep identifiers that can't be stored in roles.

    E.g., of services stored before roles; admins can update them.
    """
    response = client.put(
        f"{BASE}/services/abc", json=_service(), headers=make_headers(ADMIN)
    )
    assert response.status_code == 201
    with app.app.app_context():
        get_collection("services").update_one({"id": "abc"}, {"$set": {"id": "a,b"}})
    response = client.put(
        f"{BASE}/services/a,b",
        json=_service(name="New name"),
        headers=make_headers(ADMIN),
    )
    assert response.status_code == 200
    assert client.get(f"{BASE}/services/a,b").get_json()["name"] == "New name"


def test_codes_documented() -> None:
    """The specification and the docs document all codes of the app's checks.

    In the description of `code` of the schema `FieldError`, and in the table
    of invalid fields in `docs/api.md` (Errors).
    """
    codes = set(FIELD_ERROR_CODES)
    spec = yaml.safe_load(SPEC.read_text())
    code = spec["components"]["schemas"]["FieldError"]["properties"]["code"]
    assert codes <= set(re.findall(r"`([a-z]+(?:-[a-z]+)*)`", code["description"]))
    assert set(code["x-extensible-enum"]) == codes
    table = re.findall(r"^\| `([a-z-]+)` \|", DOCS.read_text(), flags=re.MULTILINE)
    assert codes <= set(table)
