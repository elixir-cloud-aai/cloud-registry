"""Shared fixtures.

Unit tests use a bare Flask app with the test configuration from
`tests/mock_data.py`. App tests use the app created from `config.yaml` and the
test overlay (`tests/config_overlay.yaml`), with MongoDB replaced by an
in-memory database and the identity provider by a mock; they have no network
access.
"""

from pathlib import Path
import socket
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from foca import Foca
from foca.config.access import set_app_config
from foca.database import register_mongodb
from foca.models.config import Config, MongoConfig
from foca.security import auth
import jwt
import mongomock
import pytest

from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import CUSTOM_CONFIG, DB, MONGO_CONFIG

APP_DIR = Path(__file__).parents[1] / "cloud_registry"
# Config overlay with the settings each deployment must make (audience, admin
# group)
CONFIG_OVERLAY = Path(__file__).parent / "config_overlay.yaml"
AUDIENCE = "client-id"
# The trusted issuer from `config.yaml`; all its endpoints are mocked
ISSUER = "https://login.aai.lifescience-ri.eu/oidc/"
JWKS_URI = f"{ISSUER}jwk"
USERINFO_URI = f"{ISSUER}userinfo"
USER = "alice@example.org"
ADMIN = "admin@example.org"
# Group mapped to the role `admin` in the test overlay
ADMIN_GROUP = (
    "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu"
)
# Group memberships and display names the identity provider returns as user
# info, like LS Login (not in access tokens)
ENTITLEMENTS = {
    ADMIN: [ADMIN_GROUP],
    "bob@example.org": [
        "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN#aai.lifescience-ri.eu"
    ],
}
NAMES = {
    USER: "Alice Example",
    ADMIN: "Admin Example",
    "bob@example.org": "Bob Example",
    # blank names are not stored
    "dave@example.org": " ",
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


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
    """Mock `requests.get()` for the identity provider.

    Serves the discovery, JWK set and user info endpoints; other requests
    fail.
    """
    if url.rstrip("/") == f"{ISSUER}.well-known/openid-configuration":
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
        try:
            claims = jwt.decode(
                token, KEY.public_key(), algorithms=["RS256"], audience=AUDIENCE
            )
        except jwt.InvalidTokenError:
            return MockResponse({"error": "invalid_token"}, status_code=401)
        sub = claims["sub"]
        userinfo = {"sub": sub, "eduperson_entitlement": ENTITLEMENTS.get(sub, [])}
        if sub in NAMES:
            userinfo["name"] = NAMES[sub]
        return MockResponse(userinfo)
    raise auth.requests.ConnectionError(f"Unexpected request in tests: {url}")


def make_headers(user=USER, key=KEY):
    """Request headers with an access token for a user, signed with `key`."""
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "sub": user,
            "scope": "openid eduperson_entitlement",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 300,
        },
        key,
        algorithm="RS256",
        headers={"kid": "rsa1", "typ": "at+jwt"},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def mongo_client(monkeypatch):
    """In-memory MongoDB client used by the app."""
    client = mongomock.MongoClient()
    monkeypatch.setattr(register_mongodb, "MongoClient", lambda *args, **kwargs: client)
    return client


def _no_network(sock, address):
    raise OSError(f"Network access blocked in tests: {address}")


def _clear_auth_caches():
    # FOCA caches the identity provider's responses
    for cache in [auth._oidc_configs, auth._jwk_sets, auth._userinfo_cache]:
        cache.clear()


@pytest.fixture
def mock_idp(monkeypatch):
    """Mock identity provider."""
    monkeypatch.setattr(auth.requests, "get", mock_get)
    _clear_auth_caches()
    yield
    _clear_auth_caches()


@pytest.fixture
def app_env(monkeypatch, mongo_client, mock_idp):
    """Environment for creating the app from `config.yaml` and the test
    overlay, without network.
    """
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))


@pytest.fixture
def app(app_env):
    """App created from `config.yaml` and the test overlay."""
    return Foca(
        config_file=APP_DIR / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    ).create_app()


@pytest.fixture
def client(app):
    """Test client of the app."""
    return app.app.test_client()


@pytest.fixture
def headers():
    """Request headers with a valid access token."""
    return make_headers()


@pytest.fixture
def admin_headers():
    """Request headers with a valid access token of an admin."""
    return make_headers(user=ADMIN)


@pytest.fixture
def unit_app():
    """Flask app with the test configuration, for unit tests.

    Its collections are in memory and have the unique indexes from
    `config.yaml`.
    """
    app = Flask(__name__)
    set_app_config(
        app,
        Config(db=MongoConfig(**MONGO_CONFIG), custom=CustomConfig(**CUSTOM_CONFIG)),
    )
    db = mongomock.MongoClient().db
    for name, coll_conf in app.config.foca.db.dbs[DB].collections.items():
        coll_conf.client = db[name]
        coll_conf.client.create_index("id", unique=True)
    return app


@pytest.fixture
def services(unit_app):
    """`services` collection of `unit_app`."""
    return unit_app.config.foca.db.dbs[DB].collections["services"].client
