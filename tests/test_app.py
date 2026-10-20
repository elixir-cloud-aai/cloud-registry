"""Tests for the app, with requests going through the API."""

from copy import deepcopy
import re
import string

import connexion
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
import yaml

import cloud_registry.app
from tests.conftest import APP_DIR, make_headers

BASE = "/ga4gh/registry/v1"
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
    assert response.status_code == 200, response.json
    return response.json


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/services"),
        ("get", "/services/types"),
        ("get", "/services/abc"),
        ("get", "/service-info"),
        ("post", "/services"),
        ("put", "/services/abc"),
        ("delete", "/services/abc"),
        ("post", "/service-info"),
    ],
)
def test_no_token(client, method, path):
    """Requests without an access token are rejected."""
    _assert_error(getattr(client, method)(f"{BASE}{path}", json={}), 401)


def test_invalid_token(client):
    """Requests with an access token not signed by the identity provider are
    rejected.
    """
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response = client.get(f"{BASE}/services", headers=make_headers(key=other_key))
    _assert_error(response, 401)


@pytest.mark.parametrize("path", ["/services", "/services/types"])
def test_empty_database(client, headers, path):
    """Without services, lists of services and service types are empty."""
    response = client.get(f"{BASE}{path}", headers=headers)
    assert response.status_code == 200
    assert response.json == []


def test_post_service(client, headers):
    """A posted service gets a generated identifier and can be retrieved."""
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
    ids = {_post(client, headers) for _ in range(3)}
    assert len(ids) == 3


def test_post_service_ids_exhausted(app, client, headers):
    """When no unused identifier can be generated, POST returns 500."""
    id_conf = app.app.config.foca.custom.endpoints.services.id
    id_conf.charset = "A"
    id_conf.length = 1
    assert _post(client, headers) == "A"

    response = client.post(f"{BASE}/services", json=_service(), headers=headers)
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


def test_get_service_not_found(client, headers):
    """Unknown service identifiers return 404."""
    _assert_error(client.get(f"{BASE}/services/abc", headers=headers), 404)


def test_put_service(client, headers):
    """PUT creates a service with the given identifier, then replaces it."""
    url = f"{BASE}/services/my-tes"
    response = client.put(url, json=_service(), headers=headers)
    assert response.status_code == 200
    assert response.json == "my-tes"
    assert client.get(url, headers=headers).json == _service(id="my-tes")

    response = client.put(url, json=_service(name="Your TES"), headers=headers)
    assert response.status_code == 200
    assert client.get(url, headers=headers).json == _service(
        id="my-tes", name="Your TES"
    )
    assert len(client.get(f"{BASE}/services", headers=headers).json) == 1


def test_delete_service(client, headers):
    """Deleted services are gone; deleting them again returns 404."""
    service_id = _post(client, headers)
    url = f"{BASE}/services/{service_id}"
    response = client.delete(url, headers=headers)
    assert response.status_code == 200
    assert response.json == service_id
    _assert_error(client.get(url, headers=headers), 404)
    _assert_error(client.delete(url, headers=headers), 404)


def test_get_service_types(client, headers):
    """Service types are listed once each, sorted."""
    wes = {"group": "org.ga4gh", "artifact": "wes", "version": "1.1.0"}
    for service in [_service(type=wes), _service(), _service()]:
        _post(client, headers, service)

    for _ in range(2):
        response = client.get(f"{BASE}/services/types", headers=headers)
        assert response.status_code == 200
        assert response.json == [SERVICE["type"], wes]


def test_service_info(app, client, headers):
    """Service info is registered from the config, with timestamps set on
    registration, and can be replaced.
    """
    config = app.app.config.foca.custom.endpoints.service_info.model_dump(
        exclude_none=True
    )
    response = client.get(f"{BASE}/service-info", headers=headers)
    assert response.status_code == 200
    info = response.json
    assert {k: v for k, v in info.items() if k not in TIMESTAMPS} == config
    assert info["createdAt"] == info["updatedAt"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", info["createdAt"])

    new = {**info, "name": "New name"}
    response = client.post(f"{BASE}/service-info", json=new, headers=headers)
    assert response.status_code == 201
    assert client.get(f"{BASE}/service-info", headers=headers).json == new


def test_service_info_invalid(client, headers):
    """Service info that does not match the schema is rejected."""
    response = client.post(f"{BASE}/service-info", json={}, headers=headers)
    _assert_error(response, 400)


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


def test_main(monkeypatch, mongo_client, app_env):
    """`main()` creates the app, registers service info and runs the app on
    the configured port.
    """
    ports = []
    monkeypatch.setattr(
        connexion.FlaskApp, "run", lambda self, port=None, **kwargs: ports.append(port)
    )

    cloud_registry.app.main()

    assert ports == [8080]
    service_info = mongo_client["serviceStore"]["service_info"].find_one()
    assert service_info["id"] == "ELIXIR_CLOUD_SERVICE_REGISTRY_1"


def test_main_config_missing(monkeypatch, tmp_path):
    """`main()` fails if the config file is missing."""
    monkeypatch.setattr(cloud_registry.app, "__file__", str(tmp_path / "app.py"))
    with pytest.raises(OSError):
        cloud_registry.app.main()
