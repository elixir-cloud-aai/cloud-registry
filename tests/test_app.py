"""Tests for the app, with requests going through the API."""

from copy import deepcopy
from importlib.metadata import version
import io
import json
import os
import string

import connexion
from cryptography.hazmat.primitives.asymmetric import rsa
from foca.security import auth
import pytest
import yaml

import cloud_registry.app
from cloud_registry.ga4gh.registry.service_info import InvalidServiceInfo
from cloud_registry.service_models.custom_config import MAX_CONTENT_LENGTH
from tests.conftest import (
    ADMIN_GROUP,
    APP_DIR,
    CONFIG_OVERLAY,
    make_headers,
    mock_get,
)

BASE = "/ga4gh/registry/v1"
# Public URL of the app, as configured in `config.yaml`
PUBLIC_URL = "https://0.0.0.0:8080"
TIMESTAMPS = {"createdAt", "updatedAt"}
# Unlike `tests.mock_data.MOCK_SERVICE` (for unit tests, which bypass request
# validation), this service is valid according to the API specification
SERVICE = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}


def _service(**kwargs):
    service = deepcopy(SERVICE)
    service.update(kwargs)
    return service


def _assert_error(response, status):
    assert response.status_code == status
    assert response.json["status"] == status
    assert {"title", "detail"} <= response.json.keys()


def _post(client, headers, service=None):
    response = client.post(
        f"{BASE}/services",
        json=_service() if service is None else service,
        headers=headers,
    )
    assert response.status_code == 201, response.json
    assert response.location == f"{PUBLIC_URL}{BASE}/services/{response.json}"
    return response.json


@pytest.mark.parametrize(
    "method, path, status",
    [
        ("get", "/services", 200),
        ("get", "/services/types", 200),
        ("get", "/services/abc", 404),
        ("get", "/service-info", 200),
    ],
)
def test_public_read(client, method, path, status):
    """Services and service info can be read without an access token."""
    response = getattr(client, method)(f"{BASE}{path}")
    assert response.status_code == status


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/services"),
        ("put", "/services/abc"),
        ("delete", "/services/abc"),
    ],
)
def test_no_token(client, method, path):
    """Write requests without an access token are rejected."""
    _assert_error(getattr(client, method)(f"{BASE}{path}", json={}), 401)


def test_invalid_token(client):
    """Requests with an access token not signed by the identity provider are
    rejected.
    """
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.post(
        f"{BASE}/services", json=_service(), headers=make_headers(key=other_key)
    )
    _assert_error(response, 401)


@pytest.mark.parametrize("path", ["/services", "/services/types"])
def test_empty_database(client, headers, path):
    """Without services, lists of services and service types are empty."""
    response = client.get(f"{BASE}{path}", headers=headers)
    assert response.status_code == 200
    assert response.json == []


def test_post_service(client, headers):
    """A posted service gets a generated identifier and can be retrieved at
    its location (cf. `_post`).
    """
    service_id = _post(client, headers)
    assert len(service_id) == 6
    assert set(service_id) <= set(string.ascii_uppercase + string.digits)

    response = client.get(f"{BASE}/services/{service_id}", headers=headers)
    assert response.status_code == 200
    assert response.json == _service(id=service_id)

    response = client.get(f"{BASE}/services", headers=headers)
    assert response.status_code == 200
    assert response.json == [_service(id=service_id)]


def test_post_service_ids_unique(client, headers):
    """Each posted service gets its own identifier."""
    ids = {
        _post(client, headers, _service(url=f"https://tes{i}.example.org"))
        for i in range(3)
    }
    assert len(ids) == 3


def test_post_service_ids_exhausted(app, client, headers):
    """When no unused identifier can be generated, POST returns 500."""
    id_conf = app.app.config.foca.custom.endpoints.services.id
    id_conf.charset = "A"
    id_conf.length = 1
    assert _post(client, headers) == "A"

    service = _service(url="https://other.example.org")
    response = client.post(f"{BASE}/services", json=service, headers=headers)
    _assert_error(response, 500)


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
def test_service_invalid(client, headers, method, path):
    """Services that do not match the schema are rejected."""
    service = _service()
    del service["name"]
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=service, headers=headers
    )
    _assert_error(response, 400)


def _body(size: int, suffix: bytes = b"") -> bytes:
    """A valid service as JSON, padded with whitespace to `size` bytes, and
    followed by `suffix`.
    """
    body = json.dumps(_service()).encode()
    assert len(body) <= size
    return body.ljust(size) + suffix


def _send(client, body, method="post", path="", headers=None):
    """Send a request body to `/services`."""
    return client.open(
        f"{BASE}/services{path}",
        method=method,
        data=body,
        content_type="application/json",
        headers=headers or {},
    )


def _send_chunked(client, body, headers=None):
    """Send a chunked request body (of unknown size) to `/services`, with a
    stream terminated by the server, as by Werkzeug's server; return the
    response and the number of bytes read.
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


def _record_idp_requests(monkeypatch) -> list:
    """Record the requests to the (mock) identity provider."""
    requests: list = []

    def get(url, *args, **kwargs):
        requests.append(url)
        return mock_get(url, *args, **kwargs)

    monkeypatch.setattr(auth.requests, "get", get)
    return requests


def _assert_too_large(response) -> None:
    """Assert that a response rejects a request body as too large."""
    _assert_error(response, 413)
    assert response.mimetype == "application/problem+json"
    assert response.json["title"] == "Content Too Large"


def _assert_no_services(client) -> None:
    """Assert that no services are registered (listing services is public)."""
    response = client.get(f"{BASE}/services")
    assert response.status_code == 200
    assert response.json == []


@pytest.mark.parametrize(
    "method, path, token",
    [("POST", "", False), ("POST", "", True), ("PUT", "/abc", True)],
    ids=["post-no-token", "post-token", "put-token"],
)
def test_body_too_large(monkeypatch, client, admin_headers, method, path, token):
    """Request bodies over the size limit are rejected (`413`, as problem
    details) before the access token is checked, i.e., also without a token,
    and without contacting the identity provider.
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


def test_body_too_large_declared(client):
    """Requests are rejected by their declared size (`Content-Length`),
    without reading their body.
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
def test_body_too_large_chunked(monkeypatch, client, admin_headers, body, token):
    """Chunked request bodies (of unknown size) over the size limit are
    rejected (`413`) before the access token is checked, after reading at
    most one byte more than the limit.
    """
    idp_requests = _record_idp_requests(monkeypatch)
    response, read = _send_chunked(client, body, admin_headers if token else None)
    _assert_too_large(response)
    assert read == MAX_CONTENT_LENGTH + 1
    assert idp_requests == []
    _assert_no_services(client)


@pytest.mark.parametrize("chunked", [False, True], ids=["declared", "chunked"])
def test_body_at_limit(client, headers, chunked):
    """Request bodies up to the size limit are accepted."""
    body = _body(MAX_CONTENT_LENGTH)
    if chunked:
        response, _ = _send_chunked(client, body, headers)
    else:
        response = _send(client, body, headers=headers)
    assert response.status_code == 201


def test_max_content_length_configured(monkeypatch, tmp_path, app_env, headers):
    """The size limit of request bodies is set from the config."""
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump({"custom": {"max_content_length": 1000}}))
    overlays = os.pathsep.join([str(CONFIG_OVERLAY), str(path)])
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", overlays)
    app = cloud_registry.app.create_app()
    assert app.app.config["MAX_CONTENT_LENGTH"] == 1000
    client = app.app.test_client()
    _assert_too_large(_send(client, _body(1001), headers=headers))
    _assert_too_large(_send_chunked(client, _body(1001), headers)[0])
    assert _send(client, _body(1000), headers=headers).status_code == 201


def _set_field(service: dict, field: str, value: str) -> dict:
    """Set a field of a service, given as a dotted path."""
    *parents, name = field.split(".")
    obj = service
    for parent in parents:
        obj = obj[parent]
    obj[name] = value
    return service


URL_PREFIX = "https://example.org/"


# Length limits of string fields of services (cf. README, API); values start
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
def test_service_length_limits(client, headers, field, prefix, limit):
    """String fields of services are limited in length."""

    def value(length: int) -> str:
        return prefix + "a" * (length - len(prefix))

    service = _set_field(_service(), field, value(limit + 1))
    response = client.post(f"{BASE}/services", json=service, headers=headers)
    _assert_error(response, 400)
    _post(client, headers, _set_field(_service(), field, value(limit)))


def test_get_service_not_found(client, headers):
    """Unknown service identifiers return 404."""
    _assert_error(client.get(f"{BASE}/services/abc", headers=headers), 404)


def _assert_duplicate(response, existing_id):
    """Assert that a response names the registered service with the same URL
    and API type.
    """
    assert response.status_code == 409
    assert response.mimetype == "application/problem+json"
    assert response.json["serviceId"] == existing_id
    assert existing_id in response.json["detail"]


def test_post_duplicate(client, headers):
    """A service with the same URL (in any spelling) and API type can't be
    registered again, also with another API version.
    """
    existing = _post(client, headers)
    for service in [
        _service(),
        _service(url="HTTPS://TES.example.org:443/ga4gh/tes/v1/"),
        _service(type={**SERVICE["type"], "version": "2.0.0"}),
    ]:
        response = client.post(f"{BASE}/services", json=service, headers=headers)
        _assert_duplicate(response, existing)
    services = client.get(f"{BASE}/services").json
    assert [service["id"] for service in services] == [existing]
    assert "_serviceKey" not in services[0]
    assert "_serviceKey" not in client.get(f"{BASE}/services/{existing}").json


def test_post_same_url_other_type(client, headers):
    """Services with other API types can share a URL."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    assert _post(client, headers) != _post(client, headers, _service(type=wes))


def test_put_duplicate(client, admin_headers):
    """Services can't be changed to the URL and API type of another
    service.
    """
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
        ("ftp://tes.example.org", "must be HTTP(S) URLs"),
    ],
)
def test_service_url_invalid(client, admin_headers, method, path, url, detail):
    """Invalid service URLs are rejected, saying why."""
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=_service(url=url), headers=admin_headers
    )
    _assert_error(response, 400)
    assert response.mimetype == "application/problem+json"
    assert response.json["title"] == "Bad request"
    assert detail in response.json["detail"]
    assert "Location" not in response.headers


def _service_with(field, value):
    """Service with a URL field (`organization.url` for the organization's)
    set to a value.
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
def test_service_surrogate(client, admin_headers, method, path, service, field):
    """Services with unpaired surrogates in any string, which can't be
    stored, are rejected (`400`), naming the field.
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}", json=service, headers=admin_headers
    )
    _assert_error(response, 400)
    assert f"'{field}'" in response.json["detail"]
    assert client.get(f"{BASE}/services").json == []


@pytest.mark.parametrize("field", URL_FIELDS)
def test_service_url_surrogate(client, admin_headers, field):
    """URLs with unpaired surrogates, which can't be encoded, are rejected
    (`400`), not failing later (`500`).
    """
    response = client.post(
        f"{BASE}/services",
        json=_service_with(field, "https://example.org/\ud800"),
        headers=admin_headers,
    )
    _assert_error(response, 400)
    assert client.get(f"{BASE}/services").json == []


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
@pytest.mark.parametrize("field", URL_FIELDS)
def test_service_url_not_http(client, admin_headers, method, path, field):
    """URLs of services, contacts, documentation and organizations must be
    HTTP(S) URLs (cf. the unit tests of `check_link()` for other cases).
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service_with(field, "javascript:alert(1)"),
        headers=admin_headers,
    )
    _assert_error(response, 400)
    assert response.mimetype == "application/problem+json"
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services").json == []


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
def test_service_url_http(client, admin_headers, method, path, field, value):
    """HTTP(S) URLs, with schemes in any case, are accepted and stored as
    written.
    """
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service_with(field, value),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.json
    (stored,) = client.get(f"{BASE}/services").json
    assert stored == {**_service_with(field, value), "id": response.json}


@pytest.mark.parametrize("method, path", [("post", ""), ("put", "/abc")])
def test_service_contact_url_mailto(client, admin_headers, method, path):
    """Contact URLs can also be email addresses, as in the GA4GH service info
    specification, but other links can't.
    """
    mailto = "mailto:support@example.org"
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service(contactUrl=mailto),
        headers=admin_headers,
    )
    assert response.status_code == 201, response.json
    stored = client.get(f"{BASE}/services/{response.json}").json
    assert stored["contactUrl"] == mailto
    response = getattr(client, method)(
        f"{BASE}/services{path}",
        json=_service(documentationUrl=mailto),
        headers=admin_headers,
    )
    _assert_error(response, 400)


def test_put_service(client, admin_headers):
    """PUT creates a service with the given identifier (`201`, with its
    location), then replaces it (`200`).
    """
    url = f"{BASE}/services/my-tes"
    response = client.put(url, json=_service(), headers=admin_headers)
    assert response.status_code == 201
    assert response.json == "my-tes"
    assert response.location == f"{PUBLIC_URL}{url}"
    assert client.get(url, headers=admin_headers).json == _service(id="my-tes")

    response = client.put(url, json=_service(name="Your TES"), headers=admin_headers)
    assert response.status_code == 200
    assert response.json == "my-tes"
    assert "Location" not in response.headers
    assert client.get(url, headers=admin_headers).json == _service(
        id="my-tes", name="Your TES"
    )
    assert len(client.get(f"{BASE}/services", headers=admin_headers).json) == 1


@pytest.fixture
def proxy_app(app):
    """App served behind a reverse proxy under a subpath, with one possible
    generated service identifier (`A`).
    """
    endpoints = app.app.config.foca.custom.endpoints
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
def test_location_proxy_path(proxy_app, admin_headers, method, path, location):
    """Locations of new services start with the configured path prefix and
    are percent-encoded.
    """
    client = proxy_app.app.test_client()
    response = client.open(
        f"{BASE}{path}", method=method, json=_service(), headers=admin_headers
    )
    assert response.status_code == 201
    assert response.location == location


@pytest.mark.parametrize("service_id", [".", "..", "%2E", "%2E%2E"])
def test_put_dot_segment_id(client, admin_headers, service_id):
    """Identifiers that are dot segments are rejected, as clients would
    resolve their URLs to other ones.
    """
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    _assert_error(response, 400)
    assert "Location" not in response.headers
    assert client.get(f"{BASE}/services").json == []


@pytest.mark.parametrize("service_id", ["...", ".a", "a."])
def test_put_dotted_id(client, admin_headers, service_id):
    """Identifiers with dots that aren't dot segments are accepted."""
    response = client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=admin_headers
    )
    assert response.status_code == 201
    assert response.location == f"{PUBLIC_URL}{BASE}/services/{service_id}"


def test_delete_service(client, headers, admin_headers):
    """Deleted services are gone; deleting them again returns 404."""
    service_id = _post(client, headers)
    url = f"{BASE}/services/{service_id}"
    response = client.delete(url, headers=headers)
    assert response.status_code == 200
    assert response.json == service_id
    _assert_error(client.get(url, headers=headers), 404)
    _assert_error(client.delete(url, headers=admin_headers), 404)


def test_get_service_types(client, headers):
    """Service types are listed once each, sorted."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    other = "https://other.example.org"
    for service in [_service(type=wes), _service(), _service(url=other)]:
        _post(client, headers, service)

    for _ in range(2):
        response = client.get(f"{BASE}/services/types", headers=headers)
        assert response.status_code == 200
        assert response.json == [SERVICE["type"], wes]


def test_service_info(app, client):
    """Service info is served from the config, with the package version and
    without timestamps that aren't configured.
    """
    config = app.app.config.foca.custom.endpoints.service_info
    response = client.get(f"{BASE}/service-info")
    assert response.status_code == 200
    assert response.json == {**config, "version": version("cloud-registry")}
    assert not TIMESTAMPS & response.json.keys()


def test_service_info_configured(app, client):
    """Configured versions and timestamps are served."""
    config = app.app.config.foca.custom.endpoints.service_info
    configured = {"version": "1.2.3", "createdAt": "2020-11-08T00:00:00Z"}
    config.update(configured)
    assert client.get(f"{BASE}/service-info").json.items() >= configured.items()


def test_service_info_read_only(client, admin_headers):
    """Service info can't be set via the API."""
    response = client.post(f"{BASE}/service-info", json={}, headers=admin_headers)
    assert response.status_code == 405
    spec = client.get(f"{BASE}/openapi.json").json
    assert list(spec["paths"]["/service-info"]) == ["get"]


def test_error_media_type(client, headers):
    """Error responses are problem details, a media type declared in the
    specification.
    """
    response = client.get(f"{BASE}/services/unknown", headers=headers)
    assert response.status_code == 404
    assert response.mimetype == "application/problem+json"

    spec = client.get(f"{BASE}/openapi.json").json
    declared = spec["paths"]["/services/{serviceId}"]["get"]["responses"]["404"]
    name = declared["$ref"].rsplit("/", 1)[-1]
    assert response.mimetype in spec["components"]["responses"][name]["content"]


def _refs(obj):
    """Yield the values of all `$ref` fields in a parsed specification."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "$ref":
                yield value
            else:
                yield from _refs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _refs(item)


def test_specs_local_refs():
    """The API specifications only reference schemas in the merged
    specification, so the app starts without network access.
    """
    paths = sorted((APP_DIR / "api").glob("*.yaml"))
    assert paths
    for path in paths:
        refs = list(_refs(yaml.safe_load(path.read_text())))
        assert all(ref.startswith("#/") for ref in refs), path


def _mock_run(monkeypatch) -> list:
    """Replace running the app; return the list of ports it is run on."""
    ports: list = []
    monkeypatch.setattr(
        connexion.FlaskApp, "run", lambda self, port=None, **kwargs: ports.append(port)
    )
    return ports


def _set_overlay(monkeypatch, tmp_path, overlay: dict) -> None:
    """Use a config overlay instead of the test overlay."""
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(path))


def test_main(monkeypatch, mongo_client, app_env, capsys):
    """`main()` creates the app, logs the admin groups and service info, and
    runs the app on the configured port.
    """
    ports = _mock_run(monkeypatch)
    # registered without a key, e.g., before keys were introduced
    services = mongo_client["serviceStore"]["services"]
    services.insert_one({**SERVICE, "id": "OLD"})

    cloud_registry.app.main()

    assert ports == [8080]
    assert services.find_one({"id": "OLD"})["_serviceKey"]
    # FOCA configures logging to stderr
    err = capsys.readouterr().err
    assert f"Groups mapped to the role 'admin': {ADMIN_GROUP}" in err
    assert "Serving service info 'ELIXIR_CLOUD_SERVICE_REGISTRY_1'" in err


def test_main_duplicate_services(monkeypatch, mongo_client, app_env):
    """`main()` fails if services have the same URL and API type."""
    ports = _mock_run(monkeypatch)
    services = mongo_client["serviceStore"]["services"]
    services.insert_many([{**SERVICE, "id": "FIRST"}, {**SERVICE, "id": "SECOND"}])
    with pytest.raises(RuntimeError, match="same URL and API type"):
        cloud_registry.app.main()
    assert ports == []


def test_main_requires_admin(monkeypatch, tmp_path, app_env):
    """`main()` fails if no group is mapped to the role `admin`."""
    _set_overlay(monkeypatch, tmp_path, {"security": {"auth": {"audience": ["a"]}}})
    ports = _mock_run(monkeypatch)
    with pytest.raises(RuntimeError, match="require_admin"):
        cloud_registry.app.main()
    assert ports == []


def test_main_admin_optional(monkeypatch, tmp_path, app_env, capsys):
    """Without admin groups, `main()` warns if admins are not required."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {"auth": {"audience": ["a"]}},
            "custom": {"access_control": {"require_admin": False}},
        },
    )
    ports = _mock_run(monkeypatch)
    cloud_registry.app.main()
    assert ports == [8080]
    assert "No group is mapped to the role 'admin'" in capsys.readouterr().err


def test_main_invalid_service_info(monkeypatch, tmp_path, app_env):
    """`main()` fails if the service info is invalid, naming the field."""
    _set_overlay(
        monkeypatch,
        tmp_path,
        {
            "security": {"auth": {"audience": ["a"]}},
            "custom": {
                "access_control": {"require_admin": False},
                "endpoints": {"service_info": {"contactUrl": "support"}},
            },
        },
    )
    ports = _mock_run(monkeypatch)
    with pytest.raises(
        InvalidServiceInfo, match=r"`custom\.endpoints\.service_info\.contactUrl`"
    ):
        cloud_registry.app.main()
    assert ports == []


def test_main_config_missing(monkeypatch, tmp_path):
    """`main()` fails if the config file is missing (FOCA raises `OSError`)."""
    monkeypatch.setattr(cloud_registry.app, "__file__", str(tmp_path / "app.py"))
    with pytest.raises(OSError, match=str(tmp_path / "config.yaml")):
        cloud_registry.app.main()
