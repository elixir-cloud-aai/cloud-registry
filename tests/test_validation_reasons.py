"""Tests of the `detail` and invalid fields of rejected requests (FOCA 0.22).

`config.yaml` sets `exceptions.validation_reasons`, so that `detail` is the
reason of the rejection, as up to FOCA 0.21; the invalid fields (`errors`)
never quote the rejected values. Without it (FOCA's default), `detail` names
the invalid fields by location and code, and no part of the response quotes
the values, neither for the schema validation nor for the app's own checks.
"""

from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca import get_config
import pytest

from tests.conftest import invalid_fields

BASE = "/ga4gh/registry/v1"
# Valid as per the API specification and the app's checks
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
# Rejected by the schema validation (`date-time` format)
SCHEMA_INVALID = {**SERVICE, "createdAt": "secret"}
# Rejected by the app's own checks (user info in the URL)
CHECK_INVALID = {**SERVICE, "url": "https://user:secret@tes.example.org"}


@pytest.fixture
def no_reasons(app: App) -> None:
    """Turn off `exceptions.validation_reasons`, FOCA's default."""
    with app.app.app_context():
        get_config().exceptions.validation_reasons = False


def test_reason_and_invalid_fields(
    client: FlaskClient, admin_headers: dict[str, str]
) -> None:
    """Rejected requests keep the reason as `detail` and list invalid fields.

    `exceptions.validation_reasons` keeps the validator's reason as `detail`,
    as up to FOCA 0.21; FOCA 0.22 adds the invalid fields (`errors`), with
    its problem type and code of invalid requests.
    """
    response = client.post(
        f"{BASE}/services",
        json={"url": "x"},
        headers=admin_headers,
    )
    assert response.status_code == 400
    body = response.get_json()
    assert "is a required property" in body["detail"]
    assert body["code"] == "invalid-request"
    assert body["type"].endswith("/problems/invalid-request/")
    pointers = {(error.get("pointer"), error["code"]) for error in body["errors"]}
    assert ("#/name", "required") in pointers


def test_reason_of_schema_validation(
    client: FlaskClient, admin_headers: dict[str, str]
) -> None:
    """The validator's reason may quote the value; the invalid fields don't."""
    response = client.post(
        f"{BASE}/services", json=SCHEMA_INVALID, headers=admin_headers
    )
    assert invalid_fields(response, "secret") == [("#/createdAt", "invalid-format")]
    assert "'secret'" in response.get_json()["detail"]


def test_reason_of_own_checks(
    client: FlaskClient, admin_headers: dict[str, str]
) -> None:
    """The reason of the app's checks names the fields, without the values."""
    response = client.post(
        f"{BASE}/services", json=CHECK_INVALID, headers=admin_headers
    )
    assert invalid_fields(response, "secret") == [("#/url", "invalid-url")]
    assert response.get_json()["detail"] == "'#/url': Must not contain user info."
    assert "secret" not in response.get_data(as_text=True)


@pytest.mark.usefixtures("no_reasons")
@pytest.mark.parametrize(
    "service, expected",
    [
        (SCHEMA_INVALID, ("#/createdAt", "invalid-format")),
        (CHECK_INVALID, ("#/url", "invalid-url")),
    ],
    ids=["schema", "own-checks"],
)
def test_without_reasons(
    client: FlaskClient,
    admin_headers: dict[str, str],
    service: dict[str, Any],
    expected: tuple[str, str],
) -> None:
    """Without reasons, `detail` names the fields; nothing quotes the values."""
    response = client.post(f"{BASE}/services", json=service, headers=admin_headers)
    assert invalid_fields(response, "secret") == [expected]
    pointer, code = expected
    assert response.get_json()["detail"] == f"Invalid fields: {pointer} ({code})"
    assert "secret" not in response.get_data(as_text=True)
    assert client.get(f"{BASE}/services").get_json() == []
