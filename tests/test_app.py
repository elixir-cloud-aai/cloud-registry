"""Tests for the app, with requests going through the API."""

from collections.abc import Iterator
from copy import deepcopy
from importlib.metadata import version
import inspect
import io
import json
import logging
import os
from pathlib import Path
import string
from typing import Any, NoReturn

import connexion
from connexion import App
from cryptography.hazmat.primitives.asymmetric import rsa
from flask.testing import FlaskClient
import foca.api.service_info
from foca.config.access import get_app_config
from foca.security import auth
import jwt
from pydantic import ValidationError
from pymongo import MongoClient
import pytest
from werkzeug.test import TestResponse
import yaml

import cloud_registry.app
from cloud_registry.exceptions import MAX_DETAIL_LENGTH
import cloud_registry.ga4gh.registry.server
from cloud_registry.ga4gh.registry.service import KEY, service_key
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_custom_config
from tests.conftest import (
    ADMIN_GROUP,
    APP_DIR,
    CONFIG_OVERLAY,
    ISSUER,
    USER,
    MockResponse,
    make_headers,
    mock_get,
)
from tests.mock_data import ERROR_MEDIA_TYPE, MAX_CONTENT_LENGTH

BASE = "/ga4gh/registry/v1"
# Fields of OpenAPI Path Item Objects holding Operation Objects
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
# Public URL of the app, as configured in `config.yaml`
PUBLIC_URL = "https://0.0.0.0:8080"
TIMESTAMPS = {"createdAt", "updatedAt"}
# Unlike `tests.mock_data.MOCK_SERVICE` (for unit tests, which bypass request
# validation), this service is valid according to the API specification
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}


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


def _assert_error(response: TestResponse, status: int) -> None:
    """Assert that a response is problem details with a status code.

    Args:
        response: Response.
        status: Expected status code.
    """
    assert response.status_code == status
    assert response.get_json()["status"] == status
    assert {"title", "detail"} <= response.get_json().keys()


def _post(
    client: FlaskClient,
    headers: dict[str, str],
    service: dict[str, Any] | None = None,
) -> str:
    """Register a service with `POST /services`, asserting it is created.

    Args:
        client: Test client of the app.
        headers: Request headers, with an access token.
        service: Service; `_service()` if `None`.

    Returns:
        Identifier of the registered service.
    """
    response = client.post(
        f"{BASE}/services",
        json=_service() if service is None else service,
        headers=headers,
    )
    service_id: str = response.get_json()
    assert response.status_code == 201, service_id
    assert response.location == f"{PUBLIC_URL}{BASE}/services/{service_id}"
    return service_id


def _custom_config(app: App) -> CustomConfig:
    """Get the custom config of an app.

    Args:
        app: Connexion app.

    Returns:
        Custom config.
    """
    with app.app.app_context():
        return get_custom_config()


def _service_info(app: App) -> dict[str, Any]:
    """Get the configured service info of an app.

    Args:
        app: Connexion app.

    Returns:
        Service info, as served.
    """
    service_info = get_app_config(app.app).service_info
    assert service_info is not None
    return service_info.service


@pytest.mark.parametrize(
    "method, path, status",
    [
        ("get", "/services", 200),
        ("get", "/services/types", 200),
        ("get", "/services/abc", 404),
        ("get", "/service-info", 200),
    ],
)
def test_public_read(client: FlaskClient, method: str, path: str, status: int) -> None:
    """Services and service info can be read without an access token."""
    response = getattr(client, method)(f"{BASE}{path}")
    assert response.status_code == status


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", f"{BASE}/services"),
        ("put", f"{BASE}/services/abc"),
        ("delete", f"{BASE}/services/abc"),
        ("get", f"{BASE}/me"),
        # FOCA's permissions API
        ("get", "/admin/access-control/permissions"),
    ],
)
def test_no_token(client: FlaskClient, method: str, path: str) -> None:
    """Requests without an access token are rejected with a Bearer challenge.

    That is, with `WWW-Authenticate: Bearer` (RFC 6750), for write requests
    and those that need an authenticated user.
    """
    response = getattr(client, method)(path, json={})
    _assert_error(response, 401)
    assert response.headers["WWW-Authenticate"] == "Bearer"


def _assert_invalid_token(response: TestResponse) -> None:
    """Assert that a response rejects an invalid access token.

    Args:
        response: Response.
    """
    _assert_error(response, 401)
    assert response.headers["WWW-Authenticate"].startswith(
        'Bearer error="invalid_token"'
    )


def test_invalid_token(client: FlaskClient) -> None:
    """Requests with an access token not signed by the identity provider fail."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.post(
        f"{BASE}/services", json=_service(), headers=make_headers(key=other_key)
    )
    _assert_invalid_token(response)


@pytest.mark.parametrize("typ", ["at+jwt", "application/at+jwt", "AT+JWT"])
def test_access_token_types(client: FlaskClient, typ: str) -> None:
    """Access tokens (RFC 9068) are accepted, whatever the case of their type."""
    _post(client, make_headers(typ=typ))


@pytest.mark.parametrize("typ", [None, "JWT"])
def test_other_token_types(client: FlaskClient, typ: str | None) -> None:
    """Tokens that aren't access tokens (e.g., ID tokens) are rejected.

    That is, tokens without the type of access tokens (RFC 9068), or without
    a type, even if the identity provider signed them.
    """
    headers = make_headers(typ=typ)
    token = headers["Authorization"].removeprefix("Bearer ")
    assert jwt.get_unverified_header(token).get("typ") == typ
    response = client.post(f"{BASE}/services", json=_service(), headers=headers)
    _assert_invalid_token(response)


@pytest.mark.parametrize("path", ["/services", "/services/types"])
def test_empty_database(
    client: FlaskClient, headers: dict[str, str], path: str
) -> None:
    """Without services, lists of services and service types are empty."""
    response = client.get(f"{BASE}{path}", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == []


def test_post_service(client: FlaskClient, headers: dict[str, str]) -> None:
    """A posted service gets a generated identifier.

    It can be retrieved at its location (cf. `_post`).
    """
    service_id = _post(client, headers)
    assert len(service_id) == 6
    assert set(service_id) <= set(string.ascii_uppercase + string.digits)

    response = client.get(f"{BASE}/services/{service_id}", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == _service(id=service_id)

    response = client.get(f"{BASE}/services", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == [_service(id=service_id)]


def test_post_service_ids_unique(client: FlaskClient, headers: dict[str, str]) -> None:
    """Each posted service gets its own identifier."""
    ids = {
        _post(client, headers, _service(url=f"https://tes{i}.example.org"))
        for i in range(3)
    }
    assert len(ids) == 3


def test_post_service_ids_exhausted(
    app: App, client: FlaskClient, headers: dict[str, str]
) -> None:
    """When no unused identifier can be generated, POST returns 500."""
    id_conf = _custom_config(app).endpoints.services.id
    id_conf.charset = "A"
    id_conf.length = 1
    assert _post(client, headers) == "A"

    service = _service(url="https://other.example.org")
    response = client.post(f"{BASE}/services", json=service, headers=headers)
    _assert_error(response, 500)


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
def test_service_invalid(
    client: FlaskClient, headers: dict[str, str], method: str, path: str
) -> None:
    """Services that do not match the schema are rejected."""
    service = _service()
    del service["name"]
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=service, headers=headers
    )
    _assert_error(response, 400)


def _body(size: int, suffix: bytes = b"") -> bytes:
    """Get a valid service as JSON, padded with whitespace to a size.

    Args:
        size: Size of the padded JSON, in bytes.
        suffix: Bytes appended after the padding.

    Returns:
        Request body.
    """
    body = json.dumps(_service()).encode()
    assert len(body) <= size
    return body.ljust(size) + suffix


def _send(
    client: FlaskClient,
    body: bytes,
    method: str = "post",
    path: str = "",
    headers: dict[str, str] | None = None,
) -> TestResponse:
    """Send a request body to `/services`.

    Args:
        client: Test client of the app.
        body: Request body (JSON).
        method: HTTP method.
        path: Path after `/services`.
        headers: Request headers.

    Returns:
        Response.
    """
    return client.open(
        f"{BASE}/services{path}",
        method=method,
        data=body,
        content_type="application/json",
        headers=headers or {},
    )


def _send_chunked(
    client: FlaskClient, body: bytes, headers: dict[str, str] | None = None
) -> tuple[TestResponse, int]:
    """Send a chunked request body (of unknown size) to `/services`.

    The stream is terminated by the server, as by Werkzeug's server.

    Args:
        client: Test client of the app.
        body: Request body (JSON).
        headers: Request headers.

    Returns:
        The response and the number of bytes read.
    """
    stream = io.BytesIO(body)
    response = client.post(
        f"{BASE}/services",
        input_stream=stream,
        content_type="application/json",
        headers={**(headers or {}), "Transfer-Encoding": "chunked"},
        environ_overrides={"wsgi.input_terminated": True},
    )
    return response, stream.tell()


def _record_idp_requests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the requests to the (mock) identity provider.

    Args:
        monkeypatch: Fixture to replace the requests.

    Returns:
        URLs requested so far, updated with each request.
    """
    requests: list[str] = []

    def get(url: str, *args: Any, **kwargs: Any) -> MockResponse:
        """Record a request, then send it to the mock identity provider."""
        requests.append(url)
        return mock_get(url, *args, **kwargs)

    monkeypatch.setattr(auth.requests, "get", get)
    return requests


def _assert_too_large(response: TestResponse) -> None:
    """Assert that a response rejects a request body as too large.

    Args:
        response: Response.
    """
    _assert_error(response, 413)
    assert response.content_type == ERROR_MEDIA_TYPE
    assert response.get_json()["title"] == "Content Too Large"


def _assert_no_services(client: FlaskClient) -> None:
    """Assert that no services are registered (listing services is public).

    Args:
        client: Test client of the app.
    """
    response = client.get(f"{BASE}/services")
    assert response.status_code == 200
    assert response.get_json() == []


@pytest.mark.parametrize(
    "method, path, token",
    [("POST", "", False), ("POST", "", True), ("PUT", "/abc", True)],
    ids=["post-no-token", "post-token", "put-token"],
)
def test_body_too_large(
    monkeypatch: pytest.MonkeyPatch,
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    token: bool,
) -> None:
    """Request bodies over the size limit are rejected first.

    That is, with `413`, as problem details, before the access token is
    checked, i.e., also without a token, and without contacting the identity
    provider.
    """
    idp_requests = _record_idp_requests(monkeypatch)
    response = _send(
        client,
        _body(MAX_CONTENT_LENGTH + 1),
        method=method,
        path=path,
        headers=admin_headers if token else None,
    )
    _assert_too_large(response)
    assert idp_requests == []
    _assert_no_services(client)


def test_body_too_large_declared(client: FlaskClient) -> None:
    """Requests are rejected by their declared size (`Content-Length`).

    Their body is not read.
    """
    stream = io.BytesIO(json.dumps(_service()).encode())
    response = client.post(
        f"{BASE}/services",
        input_stream=stream,
        content_type="application/json",
        environ_overrides={"CONTENT_LENGTH": str(MAX_CONTENT_LENGTH + 1)},
    )
    _assert_too_large(response)
    assert stream.tell() == 0


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/services"), ("DELETE", "/services/abc")],
)
def test_body_too_large_declared_unread(
    client: FlaskClient, method: str, path: str
) -> None:
    """Requests with a declared size over the limit are rejected (`413`).

    Also for operations that don't read their body, before the access token
    is checked.
    """
    stream = io.BytesIO(b"")
    response = client.open(
        f"{BASE}{path}",
        method=method,
        input_stream=stream,
        environ_overrides={"CONTENT_LENGTH": str(MAX_CONTENT_LENGTH + 1)},
    )
    _assert_too_large(response)


@pytest.mark.parametrize(
    "body",
    [
        _body(MAX_CONTENT_LENGTH + 1),
        # valid JSON up to the limit
        _body(MAX_CONTENT_LENGTH, suffix=b"junk"),
        json.dumps(_service(description="x" * 2 * MAX_CONTENT_LENGTH)).encode(),
    ],
    ids=["one-byte-over", "valid-up-to-limit", "twice"],
)
@pytest.mark.parametrize("token", [False, True], ids=["no-token", "token"])
def test_body_too_large_chunked(
    monkeypatch: pytest.MonkeyPatch,
    client: FlaskClient,
    admin_headers: dict[str, str],
    body: bytes,
    token: bool,
) -> None:
    """Chunked request bodies over the size limit are rejected (`413`).

    Bodies of unknown size are rejected before the access token is checked,
    after reading at most one byte more than the limit.
    """
    idp_requests = _record_idp_requests(monkeypatch)
    response, read = _send_chunked(client, body, admin_headers if token else None)
    _assert_too_large(response)
    assert read == MAX_CONTENT_LENGTH + 1
    assert idp_requests == []
    _assert_no_services(client)


@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
def test_body_at_limit(
    client: FlaskClient, headers: dict[str, str], chunked: bool
) -> None:
    """Request bodies up to the size limit are accepted."""
    body = _body(MAX_CONTENT_LENGTH)
    if chunked:
        response, _ = _send_chunked(client, body, headers)
    else:
        response = _send(client, body, headers=headers)
    assert response.status_code == 201


def test_body_content_length_invalid(client: FlaskClient) -> None:
    """Requests with an invalid declared size (`Content-Length`) fail.

    They are rejected with `400`, as problem details of the app, without
    reading their body.
    """
    stream = io.BytesIO(json.dumps(_service()).encode())
    response = client.post(
        f"{BASE}/services",
        input_stream=stream,
        content_type="application/json",
        environ_overrides={"CONTENT_LENGTH": "1e3"},
    )
    _assert_error(response, 400)
    assert response.content_type == ERROR_MEDIA_TYPE
    assert stream.tell() == 0


def test_max_content_length_default(app: App) -> None:
    """Request bodies are limited to 1 MiB by default.

    By FOCA (`server.max_content_length`).
    """
    assert get_app_config(app.app).server.max_content_length == MAX_CONTENT_LENGTH
    assert app.app.config["MAX_CONTENT_LENGTH"] == MAX_CONTENT_LENGTH


def test_max_content_length_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    headers: dict[str, str],
) -> None:
    """The size limit of request bodies is set from the config."""
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"server": {"max_content_length": 1000}}))
    overlays = os.pathsep.join([str(CONFIG_OVERLAY), str(path)])
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", overlays)
    app = cloud_registry.app.create_app()
    assert app.app.config["MAX_CONTENT_LENGTH"] == 1000
    client = app.app.test_client()
    _assert_too_large(_send(client, _body(1001), headers=headers))
    _assert_too_large(_send_chunked(client, _body(1001), headers)[0])
    assert _send(client, _body(1000), headers=headers).status_code == 201


def test_max_content_length_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """The app refuses to start with `server.max_content_length: null`.

    That would remove the size limit of request bodies (FOCA).
    """
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"server": {"max_content_length": None}}))
    overlays = os.pathsep.join([str(CONFIG_OVERLAY), str(path)])
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", overlays)
    with pytest.raises(RuntimeError, match="max_content_length"):
        cloud_registry.app.create_app()


def _set_field(service: dict[str, Any], field: str, value: str) -> dict[str, Any]:
    """Set a field of a service, given as a dotted path.

    Args:
        service: Service.
        field: Dotted path of the field (e.g., `type.group`).
        value: Value of the field.

    Returns:
        The service.
    """
    *parents, name = field.split(".")
    obj = service
    for parent in parents:
        obj = obj[parent]
    obj[name] = value
    return service


URL_PREFIX = "https://example.org/"


# Length limits of string fields of services (cf. `docs/api.md`); values start
# with the prefix (e.g., a scheme and host for URLs)
@pytest.mark.parametrize(
    "field, prefix, limit",
    [
        ("name", "", 1024),
        ("description", "", 4096),
        ("organization.name", "", 1024),
        ("organization.url", URL_PREFIX, 2048),
        ("contactUrl", URL_PREFIX, 2048),
        ("documentationUrl", URL_PREFIX, 2048),
        ("environment", "", 256),
        ("version", "", 256),
        ("url", URL_PREFIX, 2048),
        ("type.group", "", 256),
        ("type.artifact", "", 256),
        ("type.version", "", 256),
    ],
)
def test_service_length_limits(
    client: FlaskClient, headers: dict[str, str], field: str, prefix: str, limit: int
) -> None:
    """String fields of services are limited in length."""

    def value(length: int) -> str:
        """Get a value of a length, starting with the prefix."""
        return prefix + "a" * (length - len(prefix))

    service = _set_field(_service(), field, value(limit + 1))
    response = client.post(f"{BASE}/services", json=service, headers=headers)
    _assert_error(response, 400)
    _post(client, headers, _set_field(_service(), field, value(limit)))


def test_get_service_not_found(client: FlaskClient, headers: dict[str, str]) -> None:
    """Unknown service identifiers return 404."""
    _assert_error(client.get(f"{BASE}/services/abc", headers=headers), 404)


def _assert_duplicate(response: TestResponse, existing_id: str) -> None:
    """Assert that a response names the service with the same URL and API type.

    Args:
        response: Response.
        existing_id: Identifier of the registered service.
    """
    assert response.status_code == 409
    assert response.content_type == ERROR_MEDIA_TYPE
    assert response.get_json()["serviceId"] == existing_id
    assert existing_id in response.get_json()["detail"]


def test_post_duplicate(client: FlaskClient, headers: dict[str, str]) -> None:
    """A service with the same URL and API type can't be registered again.

    Also not with the URL in another spelling or with another API version.
    """
    existing = _post(client, headers)
    for service in [
        _service(),
        _service(url="HTTPS://TES.example.org:443/ga4gh/tes/v1/"),
        _service(type={**SERVICE["type"], "version": "2.0.0"}),
    ]:
        response = client.post(f"{BASE}/services", json=service, headers=headers)
        _assert_duplicate(response, existing)
    services = client.get(f"{BASE}/services").get_json()
    assert [service["id"] for service in services] == [existing]
    assert "_serviceKey" not in services[0]
    assert "_serviceKey" not in client.get(f"{BASE}/services/{existing}").get_json()


def test_post_same_url_other_type(client: FlaskClient, headers: dict[str, str]) -> None:
    """Services with other API types can share a URL."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    assert _post(client, headers) != _post(client, headers, _service(type=wes))


def test_put_duplicate(client: FlaskClient, admin_headers: dict[str, str]) -> None:
    """Services can't be changed to the URL and API type of another service."""
    existing = _post(client, admin_headers)
    other = _service(url="https://other.example.org")
    response = client.put(f"{BASE}/services/OTHER", json=other, headers=admin_headers)
    assert response.status_code == 201
    response = client.put(
        f"{BASE}/services/OTHER", json=_service(), headers=admin_headers
    )
    _assert_duplicate(response, existing)
    assert "Location" not in response.headers
    response = client.put(
        f"{BASE}/services/{existing}", json=_service(name="New"), headers=admin_headers
    )
    assert response.status_code == 200


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize(
    "url, detail",
    [
        ("https://user:secret@tes.example.org", "must not contain user info"),
        ("tes.example.org/v1", "need a scheme and a host"),
        ("http://[::1/v1", "must be valid URLs"),
        ("https://tes.exa\nmple.org", "must not contain whitespace"),
        ("ftp://tes.example.org", "must use one of these schemes: 'http', 'https'"),
        # by default (`custom.endpoints.services.url_schemes`)
        ("http://tes.example.org", "must use one of these schemes: 'https'"),
    ],
)
def test_service_url_invalid(
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    url: str,
    detail: str,
) -> None:
    """Invalid service URLs are rejected, saying why."""
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=_service(url=url), headers=admin_headers
    )
    _assert_error(response, 400)
    assert response.content_type == ERROR_MEDIA_TYPE
    assert response.get_json()["title"] == "Bad request"
    assert detail in response.get_json()["detail"]
    assert "Location" not in response.headers


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
def test_service_url_http_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    admin_headers: dict[str, str],
    method: str,
    path: str,
) -> None:
    """`http` service URLs are accepted if the config allows the scheme."""
    overlay = yaml.safe_load(CONFIG_OVERLAY.read_text())
    overlay["custom"] = {"endpoints": {"services": {"url_schemes": ["http", "https"]}}}
    _set_overlay(monkeypatch, tmp_path, overlay)
    client = cloud_registry.app.create_app().app.test_client()
    url = "http://tes.example.org/ga4gh/tes/v1"
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=_service(url=url), headers=admin_headers
    )
    assert response.status_code == 201
    service_id = response.get_json()
    assert client.get(f"{BASE}/services/{service_id}").get_json()["url"] == url


def _service_with(field: str, value: str) -> dict[str, Any]:
    """Get a service with a URL field set to a value.

    Args:
        field: URL field (`organization.url` for the organization's).
        value: Value of the field.

    Returns:
        Service.
    """
    if field == "organization.url":
        return _service(organization={**SERVICE["organization"], "url": value})
    return _service(**{field: value})


URL_FIELDS = ["url", "contactUrl", "documentationUrl", "organization.url"]


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize(
    "service, field",
    [
        (_service(name="My \ud800 TES"), "name"),
        (
            _service(type={"group": "org.\udfff", "artifact": "tes", "version": "1"}),
            "type.group",
        ),
    ],
)
def test_service_surrogate(
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    service: dict[str, Any],
    field: str,
) -> None:
    """Services with unpaired surrogates in any string are rejected (`400`).

    Such strings can't be stored; the response names the field.
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=service, headers=admin_headers
    )
    _assert_error(response, 400)
    assert f"'{field}'" in response.get_json()["detail"]
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("field", URL_FIELDS)
def test_service_url_surrogate(
    client: FlaskClient, admin_headers: dict[str, str], field: str
) -> None:
    """URLs with unpaired surrogates are rejected (`400`).

    They can't be encoded, and would otherwise fail later (`500`).
    """
    response = client.post(
        f"{BASE}/services",
        json=_service_with(field, "https://example.org/\ud800"),
        headers=admin_headers,
    )
    _assert_error(response, 400)
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize("field", URL_FIELDS)
def test_service_url_not_http(
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    field: str,
) -> None:
    """URLs of services, contacts, documentation and organizations are HTTP(S).

    Cf. the unit tests of `check_links()` for other cases.
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service_with(field, "javascript:alert(1)"),
        headers=admin_headers,
    )
    _assert_error(response, 400)
    assert response.content_type == ERROR_MEDIA_TYPE
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize(
    "field, value",
    [
        ("url", "HTTPS://TES.Example.org/ga4gh/tes/v1"),
        ("contactUrl", "https://www.example.org/contact?topic=tes"),
        ("documentationUrl", "http://docs.example.org/tes#top"),
        ("organization.url", "HttP://www.example.org:8080"),
    ],
)
def test_service_url_http(
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    field: str,
    value: str,
) -> None:
    """HTTP(S) URLs, with schemes in any case, are stored as written."""
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service_with(field, value),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.get_json()
    (stored,) = client.get(f"{BASE}/services").get_json()
    assert stored == {**_service_with(field, value), "id": response.get_json()}


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
def test_service_contact_url_mailto(
    client: FlaskClient, admin_headers: dict[str, str], method: str, path: str
) -> None:
    """Contact URLs can also be email addresses, but other links can't.

    As in the GA4GH service info specification.
    """
    mailto = "mailto:support@example.org"
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service(contactUrl=mailto),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.get_json()
    stored = client.get(f"{BASE}/services/{response.get_json()}").get_json()
    assert stored["contactUrl"] == mailto
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service(documentationUrl=mailto),
        headers=admin_headers,
    )
    _assert_error(response, 400)


def test_put_service(client: FlaskClient, admin_headers: dict[str, str]) -> None:
    """PUT creates a service with the given identifier, then replaces it.

    Creating returns `201`, with its location; replacing returns `200`.
    """
    url = f"{BASE}/services/my-tes"
    response = client.put(url, json=_service(), headers=admin_headers)
    assert response.status_code == 201
    assert response.get_json() == "my-tes"
    assert response.location == f"{PUBLIC_URL}{url}"
    assert client.get(url, headers=admin_headers).get_json() == _service(id="my-tes")

    response = client.put(url, json=_service(name="Your TES"), headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json() == "my-tes"
    assert "Location" not in response.headers
    assert client.get(url, headers=admin_headers).get_json() == _service(
        id="my-tes", name="Your TES"
    )
    assert len(client.get(f"{BASE}/services", headers=admin_headers).get_json()) == 1


@pytest.fixture
def proxy_app(app: App) -> App:
    """App served behind a reverse proxy under a subpath.

    It has one possible generated service identifier (`A`).
    """
    endpoints = _custom_config(app).endpoints
    endpoints.service.api_path = "/proxy/registry/"
    endpoints.services.id.charset = "A"
    endpoints.services.id.length = 1
    return app


@pytest.mark.parametrize(
    "method, path, location",
    [
        (
            "POST",
            "/services",
            "https://0.0.0.0:8080/proxy/registry/ga4gh/registry/v1/services/A",
        ),
        (
            "PUT",
            "/services/my-tes",
            "https://0.0.0.0:8080/proxy/registry/ga4gh/registry/v1/services/my-tes",
        ),
        (
            "PUT",
            "/services/my%20tes",
            "https://0.0.0.0:8080/proxy/registry/ga4gh/registry/v1/services/my%20tes",
        ),
    ],
)
def test_location_proxy_path(
    proxy_app: App, admin_headers: dict[str, str], method: str, path: str, location: str
) -> None:
    """Locations of new services start with the configured path prefix.

    They are percent-encoded.
    """
    client = proxy_app.app.test_client()
    response = client.open(
        f"{BASE}{path}", method=method, json=_service(), headers=admin_headers
    )
    assert response.status_code == 201
    assert response.location == location


@pytest.mark.parametrize("service_id", [".", "..", "%2E", "%2E%2E"])
def test_put_dot_segment_id(
    client: FlaskClient, admin_headers: dict[str, str], service_id: str
) -> None:
    """Identifiers that are dot segments are rejected.

    Clients would resolve their URLs to other ones.
    """
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    _assert_error(response, 400)
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("service_id", ["types", "%74ypes"])
def test_put_types_id(
    client: FlaskClient, admin_headers: dict[str, str], service_id: str
) -> None:
    """The identifier `types` is rejected.

    Its URL is that of the service types (`GET /services/types`).
    """
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    _assert_error(response, 400)
    assert client.get(f"{BASE}/services").get_json() == []


@pytest.mark.parametrize("service_id", ["Types", "types.", "atypes"])
def test_put_types_like_id(
    client: FlaskClient, admin_headers: dict[str, str], service_id: str
) -> None:
    """Identifiers other than `types` are accepted, and can be fetched."""
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    assert response.status_code == 201
    assert client.get(f"{BASE}/services/{service_id}").get_json()["id"] == service_id


@pytest.mark.parametrize("service_id", ["...", ".a", "a."])
def test_put_dotted_id(
    client: FlaskClient, admin_headers: dict[str, str], service_id: str
) -> None:
    """Identifiers with dots that aren't dot segments are accepted."""
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    assert response.status_code == 201
    assert response.location == f"{PUBLIC_URL}{BASE}/services/{service_id}"


def test_delete_service(
    client: FlaskClient, headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    """Deleted services are gone; deleting them again returns 404."""
    service_id = _post(client, headers)
    url = f"{BASE}/services/{service_id}"
    response = client.delete(url, headers=headers)
    assert response.status_code == 200
    assert response.get_json() == service_id
    _assert_error(client.get(url, headers=headers), 404)
    _assert_error(client.delete(url, headers=admin_headers), 404)


def test_get_service_types(client: FlaskClient, headers: dict[str, str]) -> None:
    """Service types are listed once each, sorted."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    other = "https://other.example.org"
    for service in [_service(type=wes), _service(), _service(url=other)]:
        _post(client, headers, service)

    for _ in range(2):
        response = client.get(f"{BASE}/services/types", headers=headers)
        assert response.status_code == 200
        assert response.get_json() == [SERVICE["type"], wes]


def test_service_info(app: App, client: FlaskClient) -> None:
    """Service info is served by FOCA from the config.

    With the package version, and without timestamps that aren't configured.
    """
    config = _service_info(app)
    response = client.get(f"{BASE}/service-info")
    assert response.status_code == 200
    assert response.get_json() == {**config, "version": version("cloud-registry")}
    assert not TIMESTAMPS & response.get_json().keys()


def test_service_info_served_by_foca(app: App) -> None:
    """`GET /service-info` is routed to FOCA's controller."""
    rule = next(
        rule
        for rule in app.app.url_map.iter_rules()
        if rule.rule == f"{BASE}/service-info"
    )
    # both unwrapped, as FOCA's controller is itself decorated (traffic
    # logging)
    view = inspect.unwrap(app.app.view_functions[rule.endpoint])
    assert view is inspect.unwrap(foca.api.service_info.getServiceInfo)


def test_service_info_traffic_logged(
    client: FlaskClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Requests for the service info and their responses are logged.

    As those of the registry's other operations (by FOCA's controller, as
    `service_info.log_traffic` is not disabled).
    """
    caplog.set_level(logging.INFO, logger="foca.utils.logging")
    client.get(f"{BASE}/service-info")
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "foca.utils.logging"
    ]
    path = f"{BASE}/service-info"
    assert any(m.startswith("Incoming request:") and path in m for m in messages)
    assert any(m.startswith("Response to request") and path in m for m in messages)


def test_service_info_configured(app: App, client: FlaskClient) -> None:
    """Configured versions and timestamps are served."""
    config = _service_info(app)
    configured = {"version": "1.2.3", "createdAt": "2020-11-08T00:00:00Z"}
    config.update(configured)
    assert client.get(f"{BASE}/service-info").get_json().items() >= configured.items()


def test_service_info_read_only(
    client: FlaskClient, admin_headers: dict[str, str]
) -> None:
    """Service info can't be set via the API."""
    response = client.post(f"{BASE}/service-info", json={}, headers=admin_headers)
    assert response.status_code == 405
    assert set(response.headers["Allow"].split(", ")) == {"GET", "HEAD", "OPTIONS"}
    spec = client.get(f"{BASE}/openapi.json").get_json()
    assert list(spec["paths"]["/service-info"]) == ["get"]


@pytest.mark.parametrize(
    "method, path, user, status",
    [
        # app exceptions, handled by FOCA
        ("get", f"{BASE}/services/UNKNWN", USER, 404),
        # request validation (`cloud_registry.exceptions.validation_problem()`)
        ("post", f"{BASE}/services", USER, 400),
        # security errors of Connexion and of FOCA's access control
        ("post", f"{BASE}/services", None, 401),
        ("delete", f"{BASE}/services/UNKNWN/verification", USER, 403),
        # FOCA's permissions API
        ("get", "/admin/access-control/permissions", None, 401),
        ("get", "/admin/access-control/permissions", USER, 403),
        # unknown routes and methods
        ("get", "/unknown", None, 404),
        ("get", f"{BASE}/unknown", USER, 404),
        ("post", f"{BASE}/service-info", None, 405),
    ],
    ids=[
        "app",
        "validation",
        "unauthorized",
        "forbidden",
        "permissions_unauthorized",
        "permissions_forbidden",
        "unknown_route",
        "unknown_api_route",
        "unknown_method",
    ],
)
def test_error_media_type(
    client: FlaskClient, method: str, path: str, user: str | None, status: int
) -> None:
    """Error responses are problem details with the GA4GH media type.

    That is, the media type declared by the GA4GH specifications, whatever
    raised them.
    """
    response = client.open(
        path,
        method=method.upper(),
        json={} if method == "post" else None,
        headers={} if user is None else make_headers(user),
    )
    assert response.status_code == status
    assert response.content_type == ERROR_MEDIA_TYPE
    assert response.get_json()["status"] == status


def test_error_media_type_problem(client: FlaskClient, headers: dict[str, str]) -> None:
    """Problem details built by the app have the media type of all errors.

    That is, those of `cloud_registry.exceptions.problem()`, e.g., for
    duplicates.
    """
    existing_id = _post(client, headers)
    response = client.post(f"{BASE}/services", json=_service(), headers=headers)
    _assert_duplicate(response, existing_id)
    assert response.content_type == ERROR_MEDIA_TYPE


def test_error_media_type_internal_error(
    monkeypatch: pytest.MonkeyPatch, client: FlaskClient
) -> None:
    """Unexpected errors have the media type of all error responses."""

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        """Fail as an unavailable database would."""
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(cloud_registry.ga4gh.registry.server, "get_collection", fail)
    response = client.get(f"{BASE}/services")
    _assert_error(response, 500)
    assert response.content_type == ERROR_MEDIA_TYPE


def _error_contents(
    spec: dict[str, Any],
) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """Yield the operation, status and content of each error response.

    References to shared responses are resolved.

    Args:
        spec: API specification.

    Yields:
        Operation, status and content (`None` if none is declared) of each
        error response, as `<METHOD> <path> <status>` and the content.
    """
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            for status, declared in operation["responses"].items():
                if status != "default" and str(status)[0] not in "45":
                    continue
                response = declared
                if "$ref" in declared:
                    name = declared["$ref"].rsplit("/", 1)[-1]
                    response = spec["components"]["responses"][name]
                yield f"{method.upper()} {path} {status}", response.get("content")


def test_error_media_type_declared(client: FlaskClient) -> None:
    """The specification declares error responses with their media type only.

    For each error response, it declares a body with only the media type of
    error responses (FOCA's permissions API declares the configured one,
    too).
    """
    response = client.get(f"{BASE}/openapi.json")
    assert response.status_code == 200
    contents = dict(_error_contents(response.get_json()))
    assert contents
    for operation, content in contents.items():
        assert content is not None, operation
        assert list(content) == [ERROR_MEDIA_TYPE], operation


def _refs(obj: object) -> Iterator[Any]:
    """Yield the values of all `$ref` fields in a parsed specification.

    Args:
        obj: Parsed specification, or part of it.

    Yields:
        Values of `$ref` fields.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _refs(item)


def test_specs_local_refs() -> None:
    """The API specifications only reference schemas in the merged one.

    So the app starts without network access.
    """
    paths = sorted((APP_DIR / "api").glob("*.yaml"))
    assert paths
    for path in paths:
        refs = list(_refs(yaml.safe_load(path.read_text())))
        assert all(ref.startswith("#/") for ref in refs), path


def _mock_run(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Replace running the app.

    Args:
        monkeypatch: Fixture to replace running the app.

    Returns:
        Ports the app is run on, updated with each run.
    """
    ports: list[int | None] = []
    monkeypatch.setattr(
        connexion.FlaskApp, "run", lambda self, port=None, **kwargs: ports.append(port)
    )
    return ports


def _set_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overlay: dict[str, Any]
) -> None:
    """Use a config overlay instead of the test overlay.

    Args:
        monkeypatch: Fixture to set the environment.
        tmp_path: Directory to write the overlay to.
        overlay: Config overlay.
    """
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(path))


def test_create_app_checks(
    mongo_client: MongoClient[dict[str, Any]],
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` logs admin groups and service info, and adds keys.

    That is, it adds missing keys of services (in each Gunicorn worker).
    """
    # registered without a key, e.g., before keys were introduced
    services = mongo_client["serviceStore"]["services"]
    services.insert_one({**SERVICE, "id": "OLD"})

    cloud_registry.app.create_app()

    stored = services.find_one({"id": "OLD"})
    assert stored is not None
    assert stored[KEY] == service_key(SERVICE)
    # FOCA configures logging to stderr
    err = capsys.readouterr().err
    assert f"Groups mapped to the role 'admin': {ADMIN_GROUP}" in err
    assert "keep_group_subjects" not in err
    assert "Serving service info 'org.example.cloud-registry'" in err
    # the test overlay keeps the placeholders
    assert "placeholder" in err
    assert "url_schemes" not in err


def test_create_app_url_schemes(
    mongo_client: MongoClient[dict[str, Any]],
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` warns of stored services with schemes not allowed.

    They are still served.
    """
    services = mongo_client["serviceStore"]["services"]
    services.insert_one({**_service(url="http://tes.example.org"), "id": "PLAIN"})
    app = cloud_registry.app.create_app()
    err = capsys.readouterr().err
    assert "url_schemes" in err
    assert "'PLAIN'" in err
    response = app.app.test_client().get(f"{BASE}/services/PLAIN")
    assert response.status_code == 200


def test_create_app_invalid_urls(
    mongo_client: MongoClient[dict[str, Any]],
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` warns of stored services with keys and invalid URLs.

    They are still served.
    """
    services = mongo_client["serviceStore"]["services"]
    services.insert_one(
        {**_service(url="https://tes.example.org/a|b"), "id": "PIPE", KEY: "PIPE"}
    )
    app = cloud_registry.app.create_app()
    err = capsys.readouterr().err
    assert "Services with invalid URLs" in err
    assert "'PIPE'" in err
    response = app.app.test_client().get(f"{BASE}/services/PIPE")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "auth",
    [
        {"service_clients": [{"client_id": "portal"}]},
        {"on_behalf_of_header": "X-On-Behalf-Of"},
    ],
    ids=["service_clients", "on_behalf_of_header"],
)
def test_create_app_service_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    auth: dict[str, Any],
) -> None:
    """`create_app()` refuses service clients, which the app doesn't support.

    FOCA would make service clients (or the users they name) owners, which
    the app's endpoints neither list nor manage.
    """
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["a"], **auth},
                "access_control": {
                    "claim_groups": "groups",
                    "group_mapping": {ADMIN_GROUP: "admin"},
                },
            }
        },
    )
    with pytest.raises(RuntimeError, match="not supported"):
        cloud_registry.app.create_app()


def test_create_app_requires_issuers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` refuses to start without trusted token issuers.

    The base config trusts none (deployments set them); FOCA refuses access
    control without them.
    """
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {
                "auth": {"audience": ["a"]},
                "access_control": {
                    "claim_groups": "groups",
                    "group_mapping": {ADMIN_GROUP: "admin"},
                },
            }
        },
    )
    with pytest.raises(ValidationError, match=r"auth\.issuers"):
        cloud_registry.app.create_app()


def test_create_app_requires_claim_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` refuses a group mapping without a group claim.

    Without `claim_groups`, no group would be admin.
    """
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["a"]},
                "access_control": {"group_mapping": {ADMIN_GROUP: "admin"}},
            }
        },
    )
    with pytest.raises(RuntimeError, match="claim_groups"):
        cloud_registry.app.create_app()


def test_create_app_no_claim_groups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` warns without a group claim, as users have no groups."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {"auth": {"issuers": [ISSUER], "audience": ["a"]}},
            "custom": {"access_control": {"require_admin": False}},
        },
    )
    cloud_registry.app.create_app()
    assert "No group claim is set" in capsys.readouterr().err


def test_create_app_group_subjects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` warns if roles of groups don't apply.

    That is, if groups are mapped, but not kept as subjects.
    """
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {
                "auth": {"issuers": [ISSUER], "audience": ["a"]},
                "access_control": {
                    "claim_groups": "groups",
                    "group_mapping": {ADMIN_GROUP: "admin"},
                    "keep_group_subjects": False,
                },
            }
        },
    )
    cloud_registry.app.create_app()
    assert "roles on services granted to groups don't apply" in (
        capsys.readouterr().err
    )


def test_create_app_dev_overlay(
    monkeypatch: pytest.MonkeyPatch, app_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`create_app()` with the Docker Compose overlay warns of no admins.

    But not about `keep_group_subjects`, which applies.
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(APP_DIR.parent / "config.dev.yaml"))
    cloud_registry.app.create_app()
    err = capsys.readouterr().err
    assert "No group is mapped to the role 'admin'" in err
    assert "keep_group_subjects" not in err
    assert "No group claim is set" not in err


def test_create_app_duplicate_services(
    mongo_client: MongoClient[dict[str, Any]], app_env: None
) -> None:
    """`create_app()` fails if services have the same URL and API type."""
    services = mongo_client["serviceStore"]["services"]
    services.insert_many([{**SERVICE, "id": "FIRST"}, {**SERVICE, "id": "SECOND"}])
    with pytest.raises(RuntimeError, match="same URL and API type"):
        cloud_registry.app.create_app()


def test_create_app_requires_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` fails if no group is mapped to the role `admin`."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {"security": {"auth": {"issuers": [ISSUER], "audience": ["a"]}}},
    )
    with pytest.raises(RuntimeError, match="require_admin"):
        cloud_registry.app.create_app()


def test_create_app_admin_optional(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without admin groups, `create_app()` warns if admins are not required."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {"auth": {"issuers": [ISSUER], "audience": ["a"]}},
            "custom": {"access_control": {"require_admin": False}},
        },
    )
    cloud_registry.app.create_app()
    assert "No group is mapped to the role 'admin'" in capsys.readouterr().err


def _service_info_overlay(service_info: dict[str, Any]) -> dict[str, Any]:
    """Get a config overlay with service info for apps without admins.

    Args:
        service_info: Service info (`service_info`).

    Returns:
        Config overlay.
    """
    return {
        "security": {"auth": {"issuers": [ISSUER], "audience": ["a"]}},
        "custom": {"access_control": {"require_admin": False}},
        "service_info": service_info,
    }


@pytest.mark.parametrize(
    "service, fields",
    [
        ({}, "`id`, `organization`"),
        ({"id": "org.example.registry"}, "`organization`"),
        ({"organization": {"name": "My organization"}}, "`id`, `organization`"),
        (
            {"id": "org.example.registry", "organization": {"name": "My org"}},
            "`organization`",
        ),
        (
            {"organization": {"name": "My org", "url": "https://my.example.org"}},
            "`id`",
        ),
    ],
)
def test_create_app_placeholder_service_info(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
    service: dict[str, Any],
    fields: str,
) -> None:
    """`create_app()` warns while placeholders of the service info are kept.

    Naming the fields still to set: `id`, and `organization` while its name or
    URL is the placeholder's.
    """
    _set_overlay(monkeypatch, tmp_path, _service_info_overlay({"service": service}))
    cloud_registry.app.create_app()
    # FOCA configures logging to stderr
    err = capsys.readouterr().err
    assert err.count("placeholder values") == 1
    assert f"placeholder values; set {fields} to" in err


def test_create_app_deployment_service_info(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    app_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`create_app()` doesn't warn if the deployment sets its service info."""
    service = {
        "id": "org.example.registry",
        "organization": {"name": "My organization", "url": "https://my.example.org"},
    }
    _set_overlay(monkeypatch, tmp_path, _service_info_overlay({"service": service}))
    cloud_registry.app.create_app()
    assert "placeholder" not in capsys.readouterr().err


def test_create_app_service_info_nulls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """Fields set to `null` in a config overlay are not served."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        _service_info_overlay({"service": {"contactUrl": None, "version": None}}),
    )
    client = cloud_registry.app.create_app().app.test_client()
    info = client.get(f"{BASE}/service-info").get_json()
    assert "contactUrl" not in info
    assert info["version"] == version("cloud-registry")


def test_create_app_invalid_service_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` fails if the service info is invalid, naming the field."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        _service_info_overlay({"service": {"contactUrl": "support"}}),
    )
    with pytest.raises(ValidationError, match=r"`service_info\.service\.contactUrl`"):
        cloud_registry.app.create_app()


def test_create_app_service_info_in_custom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` fails if the service info is set in the custom config.

    It was set there before FOCA served it (FOCA raises a `ValueError` caused
    by the validation error).
    """
    overlay = _service_info_overlay({})
    del overlay["service_info"]
    overlay["custom"]["endpoints"] = {"service_info": {"environment": "prod"}}
    _set_overlay(monkeypatch, tmp_path, overlay)
    with pytest.raises(ValueError, match="custom configuration") as exc:
        cloud_registry.app.create_app()
    assert isinstance(exc.value.__cause__, ValidationError)
    assert "endpoints.service_info" in str(exc.value.__cause__)


def test_create_app_max_content_length_in_custom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`create_app()` fails if the custom config sets a size limit of bodies.

    The size limit of request bodies was set there before FOCA limited
    request bodies (FOCA raises a `ValueError` caused by the validation
    error).
    """
    overlay = _service_info_overlay({})
    del overlay["service_info"]
    overlay["custom"]["max_content_length"] = 1000
    _set_overlay(monkeypatch, tmp_path, overlay)
    with pytest.raises(ValueError, match="custom configuration") as exc:
        cloud_registry.app.create_app()
    assert isinstance(exc.value.__cause__, ValidationError)
    assert "max_content_length" in str(exc.value.__cause__)


def test_create_app_config_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`create_app()` fails if the config file is missing (FOCA: `OSError`)."""
    monkeypatch.setattr(cloud_registry.app, "__file__", str(tmp_path / "app.py"))
    with pytest.raises(OSError, match=str(tmp_path / "config.yaml")):
        cloud_registry.app.create_app()


def test_main(monkeypatch: pytest.MonkeyPatch, app_env: None) -> None:
    """`main()` runs the app on the configured port."""
    ports = _mock_run(monkeypatch)
    cloud_registry.app.main()
    assert ports == [8080]


def test_main_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, app_env: None
) -> None:
    """`main()` does not run the app if a startup check fails."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {"security": {"auth": {"issuers": [ISSUER], "audience": ["a"]}}},
    )
    ports = _mock_run(monkeypatch)
    with pytest.raises(RuntimeError, match="require_admin"):
        cloud_registry.app.main()
    assert ports == []


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize("field", sorted(TIMESTAMPS))
@pytest.mark.parametrize(
    "value",
    ["yesterday", "2019-06-04", "2019-06-04T12:58:19", "2019-02-30T00:00:00Z"],
)
def test_service_date_time_invalid(
    client: FlaskClient,
    admin_headers: dict[str, str],
    method: str,
    path: str,
    field: str,
    value: str,
) -> None:
    """Timestamps of services must be RFC 3339 date-times.

    Others are rejected with `400`, naming the field.
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=_service(**{field: value}), headers=admin_headers
    )
    _assert_error(response, 400)
    assert f"'{field}'" in response.get_json()["detail"]
    assert client.get(f"{BASE}/services").get_json() == []


def test_service_date_time(client: FlaskClient, admin_headers: dict[str, str]) -> None:
    """Timestamps are stored as sent."""
    timestamps = {
        "createdAt": "2019-06-04T12:58:19Z",
        "updatedAt": "2026-10-20t00:00:00.5+02:00",
    }
    service_id = _post(client, admin_headers, _service(**timestamps))
    service = client.get(f"{BASE}/services/{service_id}").get_json()
    assert {field: service[field] for field in timestamps} == timestamps


@pytest.mark.parametrize(
    "request_kwargs, detail",
    [
        (
            {"method": "post", "path": "/services", "json": {"url": "x"}},
            "'name' is a required property",
        ),
        (
            {
                "method": "post",
                "path": "/services",
                "json": _service(name="a" * 1025),
            },
            "is too long - 'name'",
        ),
        (
            {"method": "put", "path": "/services/types", "json": _service()},
            "does not match",
        ),
        (
            {
                "method": "post",
                "path": "/services",
                "data": "{",
                "content_type": "application/json",
            },
            "Request body is not valid JSON",
        ),
        (
            {"method": "get", "path": "/services/abc?page=1"},
            "Extra query parameter(s) page not in spec",
        ),
    ],
    ids=["required", "too_long", "pattern", "json", "extra_parameter"],
)
def test_validation_error_detail(
    client: FlaskClient,
    admin_headers: dict[str, str],
    request_kwargs: dict[str, Any],
    detail: str,
) -> None:
    """Requests rejected by the schema validation return `400`, saying why."""
    path = request_kwargs.pop("path")
    response = client.open(f"{BASE}{path}", headers=admin_headers, **request_kwargs)
    _assert_error(response, 400)
    assert response.content_type == ERROR_MEDIA_TYPE
    assert response.get_json()["title"] == "Bad request"
    assert detail in response.get_json()["detail"]


def test_validation_error_detail_large_body(
    client: FlaskClient, admin_headers: dict[str, str]
) -> None:
    """Reasons quoting large rejected values are shortened.

    The field name is kept.
    """
    response = client.post(
        f"{BASE}/services", headers=admin_headers, json=_service(name="a" * 900_000)
    )
    _assert_error(response, 400)
    assert len(response.get_json()["detail"]) == MAX_DETAIL_LENGTH
    assert "…" in response.get_json()["detail"]
    assert response.get_json()["detail"].endswith("is too long - 'name'")


def test_validation_error_logs_bounded(
    client: FlaskClient, admin_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Large rejected values are not logged in full.

    Also not by Connexion's validation, which logs the validator's message
    before the error handler.
    """
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            f"{BASE}/services", headers=admin_headers, json=_service(name="a" * 900_000)
        )
    assert response.status_code == 400
    assert any("Request rejected" in message for message in caplog.messages)
    assert max(len(record.getMessage()) for record in caplog.records) < 2000
