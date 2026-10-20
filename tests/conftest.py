"""Pytest fixtures for cloud-registry tests."""

from copy import deepcopy
from pathlib import Path
import sys
import time
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask
from foca import Foca
from foca.models.config import Config, MongoConfig
from foca.security import auth
import flask_pymongo
import jwt
import mongomock
import pytest

from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo
from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import (
    CUSTOM_CONFIG,
    MONGO_CONFIG,
    SERVICE_INFO_CONFIG,
)

REPO_DIR = Path(__file__).resolve().parent.parent
APP_DIR = REPO_DIR / "cloud_registry"

# Ensure cloud_registry is on sys.path
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

ISSUER = "https://login.aai.lifescience-ri.eu/oidc/"
USERINFO_URI = "https://login.aai.lifescience-ri.eu/oidc/userinfo"
JWKS_URI = "https://login.aai.lifescience-ri.eu/oidc/jwk"
RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ORIGINAL_REQUESTS_GET = auth.requests.get


class MockResponse:
    """Mock response of `requests.get()`."""

    def __init__(self, data: Dict, status_code: int = 200) -> None:
        self.data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise auth.requests.HTTPError(str(self.status_code))

    def json(self) -> Dict:
        return self.data


def mock_oidc_get(url: str, headers: Optional[Dict] = None, **kwargs) -> MockResponse:
    """Mock OIDC discovery, JWK set, and userinfo endpoints."""
    if url == f"{ISSUER}.well-known/openid-configuration":
        return MockResponse(
            {
                "issuer": ISSUER,
                "jwks_uri": JWKS_URI,
                "userinfo_endpoint": USERINFO_URI,
            }
        )
    if url == JWKS_URI:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(RSA_KEY.public_key(), as_dict=True)
        return MockResponse({"keys": [{**jwk, "kid": "rsa1", "use": "sig"}]})
    if url == USERINFO_URI:
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        try:
            sub = jwt.decode(token, options={"verify_signature": False}).get("sub", "")
        except Exception:
            sub = "anonymous"
        return MockResponse({"sub": sub})
    return ORIGINAL_REQUESTS_GET(url, headers=headers, **kwargs)


def make_token(user: str = "test_user", expired: bool = False) -> str:
    """Generate a mock RS256 JWT."""
    now = int(time.time())
    exp = now - 60 if expired else now + 3600
    payload = {
        "sub": user,
        "iss": ISSUER,
        "iat": now - 10,
        "exp": exp,
        "scope": "openid",
    }
    return jwt.encode(
        payload,
        RSA_KEY,
        algorithm="RS256",
        headers={"kid": "rsa1", "typ": "at+jwt"},
    )


@pytest.fixture
def auth_headers():
    """Factory fixture for authorization headers."""

    def _headers(user: Optional[str] = "test_user", expired: bool = False) -> Dict:
        if user is None:
            return {}
        token = make_token(user=user, expired=expired)
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def mock_db():
    """In-memory mongomock database with unique indexes configured."""
    client = mongomock.MongoClient()
    db = client.serviceStore
    db.services.create_index([("id", 1)], unique=True)
    db.service_info.create_index([("id", 1)], unique=True)
    return db


@pytest.fixture
def foca_app(mock_db):
    """Flask application fixture with base FOCA config and mongomock."""
    app = Flask(__name__)
    mongo_config = MongoConfig(**deepcopy(MONGO_CONFIG))
    custom_config = CustomConfig(**deepcopy(CUSTOM_CONFIG))
    app.config.foca = Config(db=mongo_config, custom=custom_config)
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mock_db.services
    app.config.foca.db.dbs["serviceStore"].collections[
        "service_info"
    ].client = mock_db.service_info
    return app


@pytest.fixture
def client(monkeypatch):
    """Connexion test client for the full application loaded from config.yaml."""
    monkeypatch.chdir(str(REPO_DIR))
    monkeypatch.syspath_prepend(str(APP_DIR))
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)

    mongo_client = mongomock.MongoClient()
    monkeypatch.setattr(flask_pymongo, "MongoClient", lambda *a, **kw: mongo_client)
    monkeypatch.setattr(auth.requests, "get", mock_oidc_get)

    foca = Foca(
        config_file=APP_DIR / "config.yaml",
        custom_config_model="service_models.custom_config.CustomConfig",
    )
    cnx_app = foca.create_app()
    with cnx_app.app.app_context():
        service_info = RegisterServiceInfo()
        service_info.set_service_info_from_config()

    test_client = cnx_app.app.test_client()
    return test_client


@pytest.fixture
def service_payload():
    """Deep copy of a valid service registration payload."""
    return {
        "name": "My Service",
        "type": {"group": "org.ga4gh", "artifact": "beacon", "version": "1.0.0"},
        "organization": {"name": "My Organization", "url": "https://example.com"},
        "version": "1.0.0",
        "url": "https://service.example.com/api",
    }


@pytest.fixture
def service_info_payload():
    """Deep copy of a valid service-info payload."""
    return deepcopy(SERVICE_INFO_CONFIG)
