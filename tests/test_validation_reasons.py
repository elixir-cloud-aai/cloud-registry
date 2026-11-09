"""Tests of the `detail` and invalid fields of rejected requests (FOCA 0.22)."""

from flask.testing import FlaskClient

BASE = "/ga4gh/registry/v1"


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
