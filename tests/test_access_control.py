"""Tests for access control, with the app created from its config.

MongoDB is replaced by an in-memory database, and LS Login by a mock identity
provider that, like LS Login, provides group memberships via user info only.
"""

from copy import deepcopy
from pathlib import Path
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from foca import Foca
from foca.database import register_mongodb
from foca.security import auth
import jwt
import mongomock
import pytest

from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo
from tests.conftest import CONFIG_OVERLAY
from tests.mock_data import SERVICE_INFO_CONFIG

APP_DIR = Path(__file__).parents[1] / "cloud_registry"
ISSUER = "https://login.aai.lifescience-ri.eu/oidc/"
USERINFO_URI = "https://login.aai.lifescience-ri.eu/oidc/userinfo"
JWKS_URI = "https://login.aai.lifescience-ri.eu/oidc/jwk"
ADMIN_ENTITLEMENT = (
    "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu"
)
BASE = "/ga4gh/registry/v1"
SERVICE = {
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "organization": {"name": "My organization", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# `requests.get()`, for requests other than to the identity provider (e.g.,
# for references in the API specifications)
REQUESTS_GET = auth.requests.get

# Users and their entitlements
ENTITLEMENTS = {
    "alice@lifescience-ri.eu": [],
    "bob@lifescience-ri.eu": [
        "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN#aai.lifescience-ri.eu"
    ],
    "carol@lifescience-ri.eu": [],
    "admin@lifescience-ri.eu": [ADMIN_ENTITLEMENT],
}
# Display names of users (none for carol)
NAMES = {
    "alice@lifescience-ri.eu": "Alice Example",
    "bob@lifescience-ri.eu": "Bob Example",
    "admin@lifescience-ri.eu": "Admin Example",
}


class MockResponse:
    """Mock response of `requests.get()`."""

    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise auth.requests.HTTPError(str(self.status_code))

    def json(self):
        return self.data


def mock_get(url, headers=None, **kwargs):
    """Mock of LS Login's discovery, JWK set and user info endpoints."""
    if url == f"{ISSUER}.well-known/openid-configuration":
        return MockResponse(
            {
                "issuer": ISSUER,
                "jwks_uri": JWKS_URI,
                "userinfo_endpoint": USERINFO_URI,
            }
        )
    if url == JWKS_URI:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
        return MockResponse({"keys": [{**jwk, "kid": "rsa1", "use": "sig"}]})
    if url == USERINFO_URI:
        token = headers["Authorization"].removeprefix("Bearer ")
        sub = jwt.decode(token, options={"verify_signature": False})["sub"]
        userinfo = {"sub": sub, "eduperson_entitlement": ENTITLEMENTS[sub]}
        if sub in NAMES:
            userinfo["name"] = NAMES[sub]
        return MockResponse(userinfo)
    return REQUESTS_GET(url, headers=headers, **kwargs)


def headers(user=None):
    """Request headers with an access token like LS Login's for a user."""
    if user is None:
        return {}
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": "client-id",
            "sub": user,
            "scope": "openid eduperson_entitlement",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 300,
        },
        KEY,
        algorithm="RS256",
        headers={"kid": "rsa1", "typ": "at+jwt"},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(monkeypatch):
    """Client of the app, with in-memory MongoDB and mock LS Login."""
    # Modules referenced in the config are imported from the app directory
    monkeypatch.syspath_prepend(str(APP_DIR))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    mongo_client = mongomock.MongoClient()
    monkeypatch.setattr(register_mongodb, "MongoClient", lambda uri: mongo_client)
    monkeypatch.setattr(auth.requests, "get", mock_get)
    for cache in [auth._oidc_configs, auth._jwk_sets, auth._userinfo_cache]:
        cache.clear()
    app = Foca(
        config_file=APP_DIR / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    ).create_app()
    with app.app.app_context():
        RegisterServiceInfo().set_service_info_from_config()
    return app.app.test_client()


def register(client, user="alice@lifescience-ri.eu"):
    """Register a service as a user; return its identifier."""
    response = client.post(f"{BASE}/services", json=_service(), headers=headers(user))
    assert response.status_code == 200, response.json
    return response.json


def _service():
    return deepcopy(SERVICE)


def permissions(client, service_id, user="admin@lifescience-ri.eu"):
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(user)
    )
    assert response.status_code == 200, response.json
    return sorted((p["user_id"], p["role"]) for p in response.json)


def grant(client, service_id, user, role, as_user="alice@lifescience-ri.eu"):
    return client.put(
        f"{BASE}/services/{service_id}/permissions/{user}",
        json={"role": role},
        headers=headers(as_user),
    ).status_code


def update(client, service_id, user):
    return client.put(
        f"{BASE}/services/{service_id}", json=_service(), headers=headers(user)
    ).status_code


def delete(client, service_id, user):
    return client.delete(
        f"{BASE}/services/{service_id}", headers=headers(user)
    ).status_code


def test_public_read(client):
    """Anyone can read services, service types and service info."""
    service_id = register(client)
    for path in [
        "/services",
        f"/services/{service_id}",
        "/services/types",
        "/service-info",
    ]:
        response = client.get(f"{BASE}{path}")
        assert response.status_code == 200, path
    assert client.get(f"{BASE}/services/UNKNWN").status_code == 404


def test_write_requires_authentication(client):
    """Writing requires a token."""
    service_id = register(client)
    assert client.post(f"{BASE}/services", json=_service()).status_code == 401
    assert client.put(f"{BASE}/services/X", json=_service()).status_code == 401
    assert client.delete(f"{BASE}/services/{service_id}").status_code == 401
    response = client.post(f"{BASE}/service-info", json=SERVICE_INFO_CONFIG)
    assert response.status_code == 401
    response = client.get(f"{BASE}/services/{service_id}/permissions")
    assert response.status_code == 401
    assert client.get(f"{BASE}/me").status_code == 401


def test_creator_is_owner(client):
    """Any authenticated user can register a service and becomes its owner."""
    service_id = register(client, user="bob@lifescience-ri.eu")
    assert permissions(client, service_id) == [("bob@lifescience-ri.eu", "owner")]
    assert update(client, service_id, "bob@lifescience-ri.eu") == 200
    assert delete(client, service_id, "bob@lifescience-ri.eu") == 200


def test_others_forbidden(client):
    """Other users can neither modify a service nor its permissions."""
    service_id = register(client)
    bob = "bob@lifescience-ri.eu"
    assert update(client, service_id, bob) == 403
    assert delete(client, service_id, bob) == 403
    assert grant(client, service_id, bob, "owner", as_user=bob) == 403
    response = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=headers(bob)
    )
    assert response.status_code == 403
    assert client.get(f"{BASE}/services/{service_id}").status_code == 200


def test_admin(client):
    """Admins (via entitlement from user info) can modify anything."""
    service_id = register(client)
    admin = "admin@lifescience-ri.eu"
    assert update(client, service_id, admin) == 200
    assert grant(client, service_id, "carol@lifescience-ri.eu", "owner", admin) == 200
    assert delete(client, service_id, admin) == 200


def test_service_info_admin_only(client):
    """Only admins can set the service info."""
    for user, status in [
        ("alice@lifescience-ri.eu", 403),
        ("bob@lifescience-ri.eu", 403),
        ("admin@lifescience-ri.eu", 201),
    ]:
        response = client.post(
            f"{BASE}/service-info", json=SERVICE_INFO_CONFIG, headers=headers(user)
        )
        assert response.status_code == status, user


def test_maintainer(client):
    """Maintainers can update, but not delete a service or manage access."""
    service_id = register(client)
    bob = "bob@lifescience-ri.eu"
    assert grant(client, service_id, bob, "maintainer") == 200
    assert update(client, service_id, bob) == 200
    assert delete(client, service_id, bob) == 403
    assert grant(client, service_id, bob, "owner", as_user=bob) == 403
    # updating does not make maintainers owners
    assert permissions(client, service_id) == [
        ("alice@lifescience-ri.eu", "owner"),
        (bob, "maintainer"),
    ]


def test_owners_manage_permissions(client):
    """Owners can grant, change and revoke roles of other users."""
    service_id = register(client)
    bob, carol = "bob@lifescience-ri.eu", "carol@lifescience-ri.eu"
    assert grant(client, service_id, carol, "owner") == 200
    assert grant(client, service_id, bob, "maintainer", as_user=carol) == 200
    # granting a role replaces other roles
    assert grant(client, service_id, bob, "owner") == 200
    assert permissions(client, service_id, user=carol) == [
        ("alice@lifescience-ri.eu", "owner"),
        (bob, "owner"),
        (carol, "owner"),
    ]
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/{bob}",
        headers=headers("alice@lifescience-ri.eu"),
    )
    assert response.status_code == 200
    assert update(client, service_id, bob) == 403
    assert delete(client, service_id, carol) == 200


def test_invalid_permissions(client):
    """Invalid roles, unknown services and missing roles are rejected."""
    service_id = register(client)
    admin = "admin@lifescience-ri.eu"
    assert grant(client, service_id, "bob@lifescience-ri.eu", "admin") == 400
    assert grant(client, service_id, "bob,x", "owner") == 400
    assert grant(client, "UNKNWN", "bob@lifescience-ri.eu", "owner", admin) == 404
    response = client.delete(
        f"{BASE}/services/{service_id}/permissions/bob@lifescience-ri.eu",
        headers=headers("alice@lifescience-ri.eu"),
    )
    assert response.status_code == 404


def test_put_create(client):
    """Only admins can register services with chosen identifiers."""
    assert update(client, "MYSERV", "alice@lifescience-ri.eu") == 403
    assert update(client, "MYSERV", "admin@lifescience-ri.eu") == 200
    assert permissions(client, "MYSERV") == [("admin@lifescience-ri.eu", "owner")]


def test_permissions_removed_with_service(client):
    """Roles on a service are revoked when it is deleted."""
    service_id = register(client)
    assert grant(client, service_id, "bob@lifescience-ri.eu", "maintainer") == 200
    assert delete(client, service_id, "alice@lifescience-ri.eu") == 200
    # a new service with the same identifier has none of the old roles
    assert update(client, service_id, "admin@lifescience-ri.eu") == 200
    assert permissions(client, service_id) == [("admin@lifescience-ri.eu", "owner")]
    assert update(client, service_id, "bob@lifescience-ri.eu") == 403


def revoke(client, service_id, user, as_user="alice@lifescience-ri.eu"):
    return client.delete(
        f"{BASE}/services/{service_id}/permissions/{user}", headers=headers(as_user)
    ).status_code


def me(client, user):
    response = client.get(f"{BASE}/me", headers=headers(user))
    assert response.status_code == 200, response.json
    return response.json


def test_me(client):
    """Users get their identifier, admin status and roles on services."""
    alice, bob = "alice@lifescience-ri.eu", "bob@lifescience-ri.eu"
    first, second = register(client), register(client)
    assert grant(client, first, bob, "maintainer") == 200
    assert me(client, alice) == {
        "user_id": alice,
        "admin": False,
        "services": sorted(
            [
                {"service_id": first, "role": "owner"},
                {"service_id": second, "role": "owner"},
            ],
            key=lambda service: service["service_id"],
        ),
    }
    assert me(client, bob)["services"] == [{"service_id": first, "role": "maintainer"}]
    assert me(client, "carol@lifescience-ri.eu")["services"] == []
    admin = me(client, "admin@lifescience-ri.eu")
    assert (admin["admin"], admin["services"]) == (True, [])
    assert delete(client, first, alice) == 200
    assert me(client, bob)["services"] == []


def _names(client, service_id):
    response = client.get(
        f"{BASE}/services/{service_id}/permissions",
        headers=headers("admin@lifescience-ri.eu"),
    )
    return {p["user_id"]: p.get("name") for p in response.json}


def test_permission_names(client):
    """Display names are shown once users have used the registry."""
    alice, bob = "alice@lifescience-ri.eu", "bob@lifescience-ri.eu"
    carol = "carol@lifescience-ri.eu"
    service_id = register(client)
    assert grant(client, service_id, bob, "maintainer") == 200
    assert grant(client, service_id, carol, "maintainer") == 200
    # registering stores the owner's name; others are unknown so far
    assert _names(client, service_id) == {alice: NAMES[alice], bob: None, carol: None}
    me(client, bob)
    me(client, carol)  # the identity provider reports no name
    assert _names(client, service_id) == {
        alice: NAMES[alice],
        bob: NAMES[bob],
        carol: None,
    }


def test_last_owner(client):
    """The last owner of a service cannot be removed or downgraded, not even
    by admins.
    """
    alice, carol = "alice@lifescience-ri.eu", "carol@lifescience-ri.eu"
    admin = "admin@lifescience-ri.eu"
    service_id = register(client)
    assert revoke(client, service_id, alice) == 409
    assert grant(client, service_id, alice, "maintainer") == 409
    assert revoke(client, service_id, alice, as_user=admin) == 409
    assert grant(client, service_id, alice, "maintainer", as_user=admin) == 409
    assert grant(client, service_id, alice, "owner") == 200
    # with another owner, the first owner can step down
    assert grant(client, service_id, carol, "owner") == 200
    assert grant(client, service_id, alice, "maintainer") == 200
    assert revoke(client, service_id, alice, as_user=carol) == 200
    assert revoke(client, service_id, carol, as_user=carol) == 409
    assert permissions(client, service_id) == [(carol, "owner")]
