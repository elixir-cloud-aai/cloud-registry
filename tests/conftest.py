"""Shared fixtures.

Unit tests use a bare Flask app with the test configuration from
`tests/mock_data.py`. App tests use the app created from `config.yaml` and the
test overlay (`tests/config_overlay.yaml`), with the identity provider replaced
by a mock; they have no network access, except to the test database.

The tests use an in-memory database (mongomock), or, if the environment
variable `MONGODB_TEST_URI` is set, the MongoDB at that URI (e.g.,
`mongodb://localhost:27017`), as in CI: there, database `serviceStore` (`DB`)
is dropped before each test, so use a single-node MongoDB (only the hosts of
the URI may be connected to) without other data, one per test run. The tests
refuse to start if the database exists already.
"""

from collections.abc import Iterator
import os
from pathlib import Path
import socket
import time
from typing import Any

from connexion import App
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from flask.testing import FlaskClient
from foca.config.access import get_app_config, set_app_config
from foca.database import register_mongodb
from foca.models.config import (
    CollectionConfig,
    Config,
    ExceptionConfig,
    MongoConfig,
)
from foca.security import auth
import jwt
import mongomock
from mongomock.collection import Collection as MockCollection
from pymongo import MongoClient, uri_parser
from pymongo.collection import Collection
import pytest

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry.service import DATE_TIME_FIELDS
from cloud_registry.ga4gh.registry.service import KEY as SERVICE_KEY
from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import CUSTOM_CONFIG, DB, ERROR_MEDIA_TYPE, MONGO_CONFIG

APP_DIR = Path(__file__).parents[1] / "cloud_registry"
# Config overlay with the settings each deployment must make (issuer,
# audience, group claim, admin group)
CONFIG_OVERLAY = Path(__file__).parent / "config_overlay.yaml"
AUDIENCE = "client-id"
# The trusted issuer of the test overlay, a fictitious identity provider;
# all its endpoints are mocked
ISSUER = "https://login.example.org/oidc/"
JWKS_URI = f"{ISSUER}jwk"
USERINFO_URI = f"{ISSUER}userinfo"
USER = "alice@example.org"
ADMIN = "admin@example.org"
# Claim with the group memberships of users (`claim_groups` of the test
# overlay)
GROUP_CLAIM = "groups"
# Group mapped to the role `admin` in the test overlay; URN-style, so that
# colons and `#` in groups are covered (`test_group_with_slash`: slashes)
ADMIN_GROUP = "urn:example:group:registry-admins#login.example.org"
# Group of a team, whose members are users (for roles granted to groups)
TEAM_GROUP = "urn:example:group:registry-team#login.example.org"
# Group memberships and display names the identity provider returns as user
# info, not in access tokens (`userinfo_claims` of the test overlay)
GROUPS = {
    USER: [TEAM_GROUP],
    ADMIN: [ADMIN_GROUP],
    "erin@example.org": [TEAM_GROUP],
    "bob@example.org": ["urn:example:group:registry#login.example.org"],
}
NAMES = {
    USER: "Alice Example",
    ADMIN: "Admin Example",
    "bob@example.org": "Bob Example",
    # blank names are not stored
    "dave@example.org": " ",
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# URI of a MongoDB to test against instead of mongomock (see above)
MONGODB_TEST_URI = os.environ.get("MONGODB_TEST_URI", "")
# Class of the collections that the tests use, e.g., to patch their methods
COLLECTION_CLASS: type[Collection[Any]] | type[MockCollection] = (
    Collection if MONGODB_TEST_URI else MockCollection
)
_CONNECT = socket.socket.connect


def without_timestamps(service: dict[str, Any]) -> dict[str, Any]:
    """Get a service without the timestamps that the registry sets.

    Args:
        service: Service, as served.

    Returns:
        The service without `createdAt` and `updatedAt`.
    """
    return {key: value for key, value in service.items() if key not in DATE_TIME_FIELDS}


def _db_addresses(uri: str) -> frozenset[tuple[str, int]]:
    """Return the socket addresses of the hosts of a MongoDB URI.

    Args:
        uri: MongoDB connection string (`mongodb://`), or an empty string.

    Returns:
        IP address and port of each host of the URI; none for an empty string.
    """
    if not uri:
        return frozenset()
    return frozenset(
        (str(info[4][0]), port)
        for host, port in uri_parser.parse_uri(uri)["nodelist"]
        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    )


# Addresses that the tests may connect to
DB_ADDRESSES = _db_addresses(MONGODB_TEST_URI)


class MockResponse:
    """Mock response of `requests.get()`."""

    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        """Create a response.

        Args:
            data: JSON body.
            status_code: HTTP status code.
        """
        self.data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise an error for error status codes, as `requests` does.

        Raises:
            requests.HTTPError: The status code is 400 or higher.
        """
        if self.status_code >= 400:
            raise auth.requests.HTTPError(str(self.status_code))

    def json(self) -> dict[str, Any]:
        """Get the JSON body.

        Returns:
            The JSON body.
        """
        return self.data


def mock_get(
    url: str, headers: dict[str, str] | None = None, **kwargs: Any
) -> MockResponse:
    """Mock `requests.get()` for the identity provider.

    Serves the discovery, JWK set and user info endpoints; other requests
    fail.

    Args:
        url: Requested URL.
        headers: Request headers; with an access token for user info.
        kwargs: Other arguments of `requests.get()`; ignored.

    Returns:
        Response of the endpoint; status `401` for user info requests with
        an invalid access token.

    Raises:
        requests.ConnectionError: The URL is not an endpoint of the identity
            provider.
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
        assert headers is not None
        token = headers["Authorization"].removeprefix("Bearer ")
        try:
            claims = jwt.decode(
                token, KEY.public_key(), algorithms=["RS256"], audience=AUDIENCE
            )
        except jwt.InvalidTokenError:
            return MockResponse({"error": "invalid_token"}, status_code=401)
        sub = claims["sub"]
        userinfo = {"sub": sub, GROUP_CLAIM: GROUPS.get(sub, [])}
        if sub in NAMES:
            userinfo["name"] = NAMES[sub]
        return MockResponse(userinfo)
    raise auth.requests.ConnectionError(f"Unexpected request in tests: {url}")


def make_headers(
    user: str = USER, key: rsa.RSAPrivateKey = KEY, typ: str | None = "at+jwt"
) -> dict[str, str]:
    """Get request headers with an access token for a user.

    Args:
        user: User identifier (`sub` claim).
        key: Key that signs the token.
        typ: Type of the token (`typ` header), `at+jwt` for access tokens
            (RFC 9068); `None` for no type.

    Returns:
        Request headers.
    """
    now = int(time.time())
    token = jwt.encode(
        {
            "aud": AUDIENCE,
            "sub": user,
            "scope": "openid groups",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 300,
        },
        key,
        algorithm="RS256",
        # PyJWT omits `typ` if it is `None` (and sets `JWT` if it is missing)
        headers={"kid": "rsa1", "typ": typ},
    )
    return {"Authorization": f"Bearer {token}"}


def _check_no_db(client: MongoClient[dict[str, Any]]) -> None:
    """Check that a MongoDB has no database `DB`, which the tests would drop.

    Args:
        client: Client of the MongoDB.

    Ends the test run (`pytest.exit()`, as a usage error) if the database
    exists, e.g., with data of a deployment, or left by an aborted test run.
    """
    if DB in client.list_database_names():
        pytest.exit(
            f"MongoDB at MONGODB_TEST_URI has a database '{DB}', which the"
            " tests would drop: use a MongoDB without it (drop it if it is"
            " left from an aborted test run)",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


@pytest.fixture(scope="session")
def _mongodb() -> Iterator[MongoClient[dict[str, Any]] | None]:
    """Client of the MongoDB at `MONGODB_TEST_URI`, if set, for all tests.

    Fails if the MongoDB has data in database `DB` before the tests.
    """
    if not MONGODB_TEST_URI:
        yield None
        return
    client: MongoClient[dict[str, Any]] = MongoClient(
        MONGODB_TEST_URI, serverSelectionTimeoutMS=5000
    )
    try:
        _check_no_db(client)
        yield client
        client.drop_database(DB)
    finally:
        client.close()


@pytest.fixture
def db_client(
    _mongodb: MongoClient[dict[str, Any]] | None,
) -> MongoClient[dict[str, Any]]:
    """MongoDB client of a test, with an empty database `DB`.

    The MongoDB at `MONGODB_TEST_URI` if set, otherwise a new in-memory one.
    """
    if _mongodb is None:
        client: MongoClient[dict[str, Any]] = mongomock.MongoClient()
        return client
    _mongodb.drop_database(DB)
    return _mongodb


@pytest.fixture
def mongo_client(
    monkeypatch: pytest.MonkeyPatch, db_client: MongoClient[dict[str, Any]]
) -> MongoClient[dict[str, Any]]:
    """MongoDB client used by the app (see `db_client`)."""
    monkeypatch.setattr(
        register_mongodb, "MongoClient", lambda *args, **kwargs: db_client
    )
    return db_client


def _no_network(sock: socket.socket, address: object) -> None:
    """Replace `socket.socket.connect()`, to block network access.

    Connections to the test database (`DB_ADDRESSES`) are allowed.

    Args:
        sock: Socket to connect.
        address: Address to connect to.

    Raises:
        OSError: If the address is not one of the test database.
    """
    if isinstance(address, tuple) and tuple(address[:2]) in DB_ADDRESSES:
        _CONNECT(sock, address)
        return
    raise OSError(f"Network access blocked in tests: {address}")


def _clear_auth_caches() -> None:
    """Clear FOCA's caches of the identity provider's responses."""
    auth._oidc_configs.clear()
    auth._jwk_sets.clear()
    auth._userinfo_cache.clear()


@pytest.fixture
def mock_idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Mock identity provider."""
    monkeypatch.setattr(auth.requests, "get", mock_get)
    _clear_auth_caches()
    yield
    _clear_auth_caches()


@pytest.fixture
def app_env(
    monkeypatch: pytest.MonkeyPatch,
    mongo_client: MongoClient[dict[str, Any]],
    mock_idp: None,
) -> None:
    """Environment for creating the app, without network.

    The app is created from `config.yaml` and the test overlay.
    """
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))


@pytest.fixture
def app(app_env: None) -> App:
    """App created from `config.yaml` and the test overlay."""
    return create_app()


@pytest.fixture
def client(app: App) -> FlaskClient:
    """Test client of the app."""
    return app.app.test_client()


@pytest.fixture
def headers() -> dict[str, str]:
    """Request headers with a valid access token."""
    return make_headers()


@pytest.fixture
def admin_headers() -> dict[str, str]:
    """Request headers with a valid access token of an admin."""
    return make_headers(user=ADMIN)


def _collections(app: Flask) -> dict[str, CollectionConfig]:
    """Get the configs of the collections of the service store of an app.

    Args:
        app: Flask app with FOCA's config.

    Returns:
        Configs of the collections, by name.
    """
    mongo = get_app_config(app).db
    assert mongo is not None and mongo.dbs is not None
    collections = mongo.dbs[DB].collections
    assert collections is not None
    return collections


@pytest.fixture
def unit_app(db_client: MongoClient[dict[str, Any]]) -> Flask:
    """Flask app with the test configuration, for unit tests.

    Its collections are those of database `DB` of `db_client`, with the unique
    indexes from `config.yaml`; error responses have the media type of
    `config.yaml`.
    """
    app = Flask(__name__)
    set_app_config(
        app,
        Config.model_validate(
            {
                "db": MongoConfig.model_validate(MONGO_CONFIG),
                "exceptions": ExceptionConfig(media_type=ERROR_MEDIA_TYPE),
                "custom": CustomConfig.model_validate(CUSTOM_CONFIG),
            }
        ),
    )
    db = db_client[DB]
    for name, coll_conf in _collections(app).items():
        coll_conf.client = db[name]
        coll_conf.client.create_index("id", unique=True)
    db["services"].create_index(SERVICE_KEY, unique=True, sparse=True)
    return app


@pytest.fixture
def services(unit_app: Flask) -> Collection[dict[str, Any]]:
    """`services` collection of `unit_app`."""
    collection = _collections(unit_app)["services"].client
    assert collection is not None
    return collection
