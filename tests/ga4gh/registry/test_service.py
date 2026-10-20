"""Test cases for service registration."""

from copy import deepcopy

from pymongo.errors import DuplicateKeyError
import pytest

from cloud_registry.exceptions import InternalServerError
from cloud_registry.ga4gh.registry.service import (
    KEY,
    DuplicateService,
    InvalidService,
    RegisterService,
    _normalize_host,
    add_service_keys,
    normalize_url,
    service_key,
)
from tests.mock_data import (
    MOCK_ID,
    MOCK_ID_ONE_CHAR,
    MOCK_SERVICE,
    MOCK_TYPE,
)


def _without_db_id(doc):
    del doc["_id"]
    return doc


def _stored(data: dict, id: str) -> dict:
    """Expected document of a stored service."""
    return {**data, "id": id, KEY: service_key(data)}


class TestRegisterService:
    """Tests for `RegisterService` class."""

    @pytest.mark.parametrize("service_id, replace", [(None, False), (MOCK_ID, True)])
    def test_init(self, unit_app, service_id, replace):
        """Test for constructing class, with and without an identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=service_id)
            assert obj.data["name"] == MOCK_SERVICE["name"]
            assert obj.data["id"] == service_id
            assert obj.replace is replace

    def test_register_metadata(self, unit_app, services):
        """Test for registering a service with a randomly assigned identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert isinstance(obj.data["id"], str)
        stored = _without_db_id(services.find_one({"id": obj.data["id"]}))
        assert stored == _stored(MOCK_SERVICE, obj.data["id"])

    def test_register_metadata_literal_id_charset(self, unit_app):
        """Test for registering a service with a randomly assigned identifier
        generated from a literal character set.
        """
        id_conf = unit_app.config.foca.custom.endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID_ONE_CHAR

    def test_register_metadata_with_id(self, unit_app, services):
        """Test for registering a service with a user-supplied identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(MOCK_SERVICE, MOCK_ID)
        assert services.count_documents({}) == 1

    def test_register_metadata_with_id_replace(self, unit_app, services):
        """Test for updating an existing service."""
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID})

        data = {**MOCK_SERVICE, "name": "new name"}
        with unit_app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(data, MOCK_ID)
        assert services.count_documents({}) == 1

    def test_register_metadata_duplicate_key_new_id(
        self, monkeypatch, unit_app, services
    ):
        """Test for registering a service; a new identifier is generated after
        a duplicate key error.
        """
        services.insert_one({"id": "AAAAAA"})
        ids = iter(["AAAAAA", "BBBBBB"])
        monkeypatch.setattr(
            "cloud_registry.ga4gh.registry.service.generate_id",
            lambda **kwargs: next(ids),
        )

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == "BBBBBB"
        assert services.find_one({"id": "BBBBBB"})["name"] == MOCK_SERVICE["name"]

    def test_register_metadata_duplicate_keys_repeated(self, unit_app, services):
        """Test for registering a service; running out of unique identifiers."""
        id_conf = unit_app.config.foca.custom.endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1
        services.insert_one({"id": MOCK_ID_ONE_CHAR})

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            with pytest.raises(InternalServerError):
                obj.register_metadata()
        assert services.count_documents({}) == 1

    def test_register_metadata_does_not_modify_data(self, unit_app):
        """Test that registering a service does not modify the caller's data."""
        data = deepcopy(MOCK_SERVICE)

        with unit_app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            RegisterService(data=data, id=obj.data["id"]).register_metadata()
        assert data == MOCK_SERVICE


@pytest.mark.parametrize(
    "url, normalized",
    [
        (
            "HTTPS://TES.Example.org:443/ga4gh/tes/v1/",
            "https://tes.example.org/ga4gh/tes/v1",
        ),
        ("http://tes.example.org:80/", "http://tes.example.org"),
        ("https://tes.example.org/v1?x=1#top", "https://tes.example.org/v1"),
        ("https://tes.example.org:8443/V1", "https://tes.example.org:8443/V1"),
        ("https://[::1]:443/v1", "https://[::1]/v1"),
        ("https://[::1]:8443/v1", "https://[::1]:8443/v1"),
        (" https://tes.example.org ", "https://tes.example.org"),
        ("https://tes.example.org./v1", "https://tes.example.org/v1"),
        (
            "https://tes.example.org//ga4gh/./tes/x/../v1",
            "https://tes.example.org/ga4gh/tes/v1",
        ),
        ("https://tes.example.org/../v1", "https://tes.example.org/v1"),
        ("https://tes.example.org/a/%2e%2E/v1", "https://tes.example.org/v1"),
        ("https://tes.example.org.:8443/v1", "https://tes.example.org:8443/v1"),
        ("https://tes.example.org/%74es/a%2fb", "https://tes.example.org/tes/a%2Fb"),
        ("https://bücher.example.org", "https://xn--bcher-kva.example.org"),
        ("https://XN--BCHER-KVA.example.org", "https://xn--bcher-kva.example.org"),
        ("https://faß.de", "https://xn--fa-hia.de"),
        ("https://a.com。/x", "https://a.com/x"),
        ("https://my_host.internal", "https://my_host.internal"),
        ("https://[0::FFFF:1.2.3.4]/v1", "https://[::ffff:1.2.3.4]/v1"),
        ("https://[fe80::1%25eth0]", "https://[fe80::1%25eth0]"),
        ("https://tes.example.org/ü", "https://tes.example.org/%C3%BC"),
        ("http://tes.example.org:443", "http://tes.example.org:443"),
    ],
)
def test_normalize_url(url, normalized):
    """URLs are compared with lowercase schemes and hosts, encoded host
    names, and without trailing dots of hosts, default ports, dot segments,
    repeated and trailing slashes, needless percent-encoding, queries and
    fragments.
    """
    assert normalize_url(url) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "tes.example.org/v1",
        "mailto:support@example.org",
        "https://user:secret@tes.example.org",
        "https://user@tes.example.org",
        "https://tes.example.org:99999",
        "https://tes.example.org:port",
        "http://[::1/v1",
        "https://.",
        "https://[::g]",
        f"https://{'ü' * 64}.example.org",
    ],
)
def test_normalize_url_invalid(url):
    """Invalid URLs, URLs without scheme or host, with user info or invalid
    ports or hosts are rejected.
    """
    with pytest.raises(InvalidService):
        normalize_url(url)


def test_normalize_host_invalid_ipv6():
    """Invalid IPv6 addresses are rejected (`urlsplit()` rejects most)."""
    with pytest.raises(InvalidService):
        _normalize_host("::g")


def test_service_key():
    """Keys ignore the case of the type, surrounding whitespace and its
    version.
    """
    other = {
        **MOCK_SERVICE,
        "type": {"group": "ORG.GA4GH ", "artifact": "Beacon", "version": "2.0.0"},
        "url": "https://BEACON.example.org/api/",
    }
    assert service_key(other) == service_key(MOCK_SERVICE)
    wes = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "artifact": "wes"}}
    assert service_key(wes) != service_key(MOCK_SERVICE)


@pytest.mark.parametrize(
    "data",
    [
        {"type": MOCK_TYPE},
        {"url": "https://tes.example.org"},
        {"url": "https://tes.example.org", "type": "tes"},
        {"url": 1, "type": MOCK_TYPE},
        {"url": "https://tes.example.org", "type": {**MOCK_TYPE, "group": 1}},
    ],
)
def test_service_key_invalid(data):
    """Services need a URL and a type, as strings."""
    with pytest.raises(InvalidService):
        service_key(data)


class TestDuplicates:
    """Tests for rejecting services with the same URL and API type."""

    def _register(self, unit_app, data=MOCK_SERVICE, id=None):
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(data), id=id)
            obj.register_metadata()
        return obj.data["id"]

    def test_post(self, unit_app, services):
        """Registering a service again fails, naming the registered one."""
        existing = self._register(unit_app)
        variant = {**MOCK_SERVICE, "url": "HTTPS://Beacon.Example.org:443/api/"}
        with pytest.raises(DuplicateService) as exc_info:
            self._register(unit_app, data=variant)
        assert exc_info.value.existing_id == existing
        assert existing in exc_info.value.description
        assert services.count_documents({}) == 1

    def test_put(self, unit_app, services):
        """Services can't be changed to the URL and type of another one."""
        self._register(unit_app)
        other = {**MOCK_SERVICE, "url": "https://other.example.org"}
        self._register(unit_app, data=other, id=MOCK_ID)
        with pytest.raises(DuplicateService):
            self._register(unit_app, id=MOCK_ID)
        assert services.find_one({"id": MOCK_ID})["url"] == other["url"]

    def test_put_same_service(self, unit_app):
        """Services can be replaced with the same URL and type, also with a
        new API version.
        """
        existing = self._register(unit_app)
        new_version = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "version": "2.0.0"}}
        assert self._register(unit_app, data=new_version, id=existing) == existing

    @pytest.mark.parametrize("id", [None, MOCK_ID])
    def test_concurrent(self, monkeypatch, unit_app, services, id):
        """Services registered concurrently, after the check, are named."""
        check = RegisterService._check_duplicate
        calls = []

        def check_later(self, key):
            # the service is registered concurrently after the first check
            calls.append(key)
            if len(calls) > 1:
                check(self, key)

        services.insert_one(
            {**MOCK_SERVICE, "id": "OTHER", KEY: service_key(MOCK_SERVICE)}
        )
        monkeypatch.setattr(RegisterService, "_check_duplicate", check_later)
        with pytest.raises(DuplicateService) as exc_info:
            self._register(unit_app, id=id)
        assert exc_info.value.existing_id == "OTHER"

    def test_put_other_duplicate_key_error(self, monkeypatch, unit_app):
        """Duplicate key errors of a PUT not caused by another service with
        the same key are raised (e.g., if the same identifier is registered
        concurrently).
        """

        def fail(*args, **kwargs):
            raise DuplicateKeyError("id")

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "replace_one", fail)
            with pytest.raises(DuplicateKeyError):
                obj.register_metadata()


class TestAddServiceKeys:
    """Tests for adding keys to services registered without one."""

    def test_add(self, unit_app, services):
        """Keys are added to services without one."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD"})
        existing = {**MOCK_SERVICE, "id": "NEW", "url": "https://new.example.org"}
        services.insert_one({**existing, KEY: "kept"})
        with unit_app.app_context():
            add_service_keys()
        assert services.find_one({"id": "OLD"})[KEY] == service_key(MOCK_SERVICE)
        assert services.find_one({"id": "NEW"})[KEY] == "kept"

    def test_concurrent(self, monkeypatch, unit_app, services):
        """Services registered concurrently with the same key are named."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD"})

        def fail(*args, **kwargs):
            raise DuplicateKeyError(KEY)

        monkeypatch.setattr(services, "update_one", fail)
        with (
            unit_app.app_context(),
            pytest.raises(RuntimeError, match="'OLD': same URL and API type"),
        ):
            add_service_keys()

    def test_problems(self, unit_app, services):
        """All services with the same URL and API type as another one, or
        with invalid URLs, are named.
        """
        other = {**MOCK_SERVICE, "url": "https://other.example.org"}
        for id, data in [("A", MOCK_SERVICE), ("B", MOCK_SERVICE), ("C", other)]:
            services.insert_one({**data, "id": id})
        services.insert_one({**other, "id": "D"})
        services.insert_one({**MOCK_SERVICE, "id": "BAD", "url": "bad"})
        with unit_app.app_context(), pytest.raises(RuntimeError) as exc_info:
            add_service_keys()
        message = str(exc_info.value)
        assert "'B': same URL and API type as 'A'" in message
        assert "'D': same URL and API type as 'C'" in message
        assert "'BAD': Service URLs need a scheme and a host." in message
