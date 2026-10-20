"""Test cases for service registration."""

from copy import deepcopy
from datetime import datetime

from pymongo.errors import DuplicateKeyError
import pytest

from cloud_registry.exceptions import InternalServerError
from cloud_registry.ga4gh.registry.service import (
    KEY,
    VERIFIED,
    DuplicateService,
    InvalidService,
    RegisterService,
    _normalize_host,
    add_service_keys,
    check_link,
    check_links,
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

    def test_register_metadata_dot_ids(self, monkeypatch, unit_app):
        """Generated identifiers `.` and `..` are replaced."""
        ids = iter([".", "..", "A"])
        monkeypatch.setattr(
            "cloud_registry.ga4gh.registry.service.generate_id",
            lambda **kwargs: next(ids),
        )
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == "A"

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
        "javascript://tes.example.org/%0Aalert(1)",
        "file://tes.example.org/etc/passwd",
        "ftp://tes.example.org/v1",
        "https://user:secret@tes.example.org",
        "https://user@tes.example.org",
        "https://tes.example.org:99999",
        "https://tes.example.org:port",
        "http://[::1/v1",
        "https://.",
        "https://[::g]",
        f"https://{'ü' * 64}.example.org",
        "https://tes example.org",
        "https://tes<x>.example.org",
        "https://tes%zz.example.org",
        "https://tes.example.org/a%ZZ",
        "https://tes.example.org/a%2",
    ],
)
def test_normalize_url_invalid(url):
    """Invalid URLs, URLs without scheme or host, with schemes other than
    `http` and `https`, user info or invalid ports or hosts are rejected.
    """
    with pytest.raises(InvalidService):
        normalize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.example.org",
        "HTTPS://WWW.Example.org:8443/docs?page=1#top",
        "https://[::1]/docs",
        "https://bücher.example.org",
    ],
)
def test_check_link(url):
    """HTTP(S) URLs with valid hosts are valid links."""
    check_link(url, field="documentationUrl")


@pytest.mark.parametrize(
    "url, detail",
    [
        (1, "must be a string"),
        ("https://www.example.org/a b", "must not contain whitespace"),
        ("https://www.example.org/\x00", "must not contain whitespace"),
        ("https://www.example.org:port", "Invalid URL"),
        ("http://[::1/docs", "Invalid URL"),
        ("javascript:alert(1)", "must be an HTTP(S) URL"),
        ("mailto:support@example.org", "must be an HTTP(S) URL"),
        ("www.example.org/docs", "must be an HTTP(S) URL"),
        ("https:///docs", "need a host"),
        ("https://user@www.example.org", "must not contain user info"),
        ("https://www<x>.example.org", "Invalid host"),
    ],
)
def test_check_link_invalid(url, detail):
    """Links must be HTTP(S) URLs with a valid host and port, and without
    user info or whitespace.
    """
    with pytest.raises(InvalidService) as exc_info:
        check_link(url, field="documentationUrl")
    assert detail in exc_info.value.description
    assert "'documentationUrl'" in exc_info.value.description


def test_check_link_schemes():
    """Links can have other allowed schemes, with a path but no host."""
    check_link(
        "MAILTO:support@example.org", field="contactUrl", schemes=frozenset({"mailto"})
    )
    for url in ["mailto:", "mailto://example.org/support"]:
        with pytest.raises(InvalidService):
            check_link(url, field="contactUrl", schemes=frozenset({"mailto"}))


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"organization": "organization"},
        {"organization": {"name": "My organization"}},
        {
            "contactUrl": "mailto:support@example.org",
            "documentationUrl": "https://docs.example.org",
            "organization": {"url": "https://example.org"},
        },
    ],
)
def test_check_links(data):
    """Missing links are not checked, and valid links are accepted."""
    check_links(data)


@pytest.mark.parametrize(
    "data",
    [
        {"contactUrl": "javascript:alert(1)"},
        {"documentationUrl": "mailto:support@example.org"},
        {"organization": {"url": "file:///etc/passwd"}},
    ],
)
def test_check_links_invalid(data):
    """Invalid links are rejected."""
    with pytest.raises(InvalidService):
        check_links(data)


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


def test_service_key_unambiguous():
    """Types whose fields join to the same text have different keys."""
    first = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "group": "a b", "artifact": "c"}}
    second = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "group": "a", "artifact": "b c"}}
    assert service_key(first) != service_key(second)


def test_service_key_casefold():
    """Types are compared with Unicode case folding."""
    first = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "artifact": "straße"}}
    second = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "artifact": "STRASSE"}}
    assert service_key(first) == service_key(second)


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

    def test_put_added_concurrently(self, monkeypatch, unit_app, services):
        """Services added concurrently with the same identifier (duplicate
        key errors not caused by another service with the same key) are read
        again and replaced.
        """
        replace_one = services.replace_one
        calls = []

        def add_first(*args, **kwargs):
            # the service is added concurrently before the first upsert
            calls.append(kwargs["upsert"])
            if len(calls) == 1:
                services.insert_one({**MOCK_SERVICE, "id": MOCK_ID, "name": "old"})
                raise DuplicateKeyError("id")
            return replace_one(*args, **kwargs)

        new = {**MOCK_SERVICE, "name": "new"}
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(new), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "replace_one", add_first)
            obj.register_metadata()
        assert calls == [True, False]
        assert not obj.was_created
        assert services.find_one({"id": MOCK_ID})["name"] == "new"
        assert services.count_documents({}) == 1

    def test_put_duplicate_key_error_on_each_try(self, monkeypatch, unit_app):
        """If duplicate key errors not caused by another service with the
        same key persist, registering fails.
        """

        def fail(*args, **kwargs):
            raise DuplicateKeyError("id")

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "replace_one", fail)
            with pytest.raises(InternalServerError):
                obj.register_metadata(retries=2)


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

    def test_replaced_concurrently(self, monkeypatch, unit_app, services):
        """Keys written concurrently, e.g., by a replacement, are kept."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD", KEY: "newer"})
        find = services.find

        def find_without_key(filter=None, *args, **kwargs):
            # serve the service as read before its replacement
            if filter == {KEY: {"$exists": False}}:
                return find({}, {"_id": False, KEY: False})
            return find(filter, *args, **kwargs)

        monkeypatch.setattr(services, "find", find_without_key)
        with unit_app.app_context():
            add_service_keys()
        assert services.find_one({"id": "OLD"})[KEY] == "newer"

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


class TestVerificationReset:
    """Tests for keeping and removing verifications on replacements."""

    TIME = datetime(2026, 10, 20, 12, 0)

    def _verified(self, services, data=MOCK_SERVICE):
        services.insert_one(
            {**data, "id": MOCK_ID, KEY: service_key(data), VERIFIED: self.TIME}
        )

    def _replace(self, unit_app, data):
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(data), id=MOCK_ID)
            obj.register_metadata()
        assert not obj.was_created

    @pytest.mark.parametrize(
        "change",
        [
            {"name": "new name", "version": "2.0.0"},
            {"type": {**MOCK_TYPE, "version": "2.0.0"}},
            {"type": {**MOCK_TYPE, "artifact": "BEACON"}},
        ],
    )
    def test_kept(self, unit_app, services, change):
        """Verifications are kept if the URL (as written) and the key are
        unchanged.
        """
        self._verified(services)
        data = {**MOCK_SERVICE, **change}
        self._replace(unit_app, data)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == {**_stored(data, MOCK_ID), VERIFIED: self.TIME}

    @pytest.mark.parametrize(
        "change",
        [
            {"url": "https://other.example.org/api"},
            {"type": {**MOCK_TYPE, "artifact": "tes"}},
            {"type": {**MOCK_TYPE, "group": "org.example"}},
            # same key, but the URL is written differently
            {"url": "HTTPS://beacon.example.org/api/"},
            {"url": "https://beacon.example.org/api?redirect=evil"},
            {"url": "https://beacon.example.org/api#evil"},
        ],
    )
    def test_removed(self, unit_app, services, change):
        """Verifications are removed if the URL (in any way) or the key
        changes.
        """
        self._verified(services)
        data = {**MOCK_SERVICE, **change}
        self._replace(unit_app, data)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(data, MOCK_ID)

    def test_without_key(self, unit_app, services):
        """Services without a key (cf. `add_service_keys()`) are replaced,
        without their verification.
        """
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID, VERIFIED: self.TIME})
        self._replace(unit_app, MOCK_SERVICE)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(MOCK_SERVICE, MOCK_ID)

    @pytest.mark.parametrize(
        "concurrent, kept",
        [
            # the URL changes, and the service is verified again
            (
                {
                    "$set": {
                        "url": "https://other.example.org",
                        KEY: "other",
                        VERIFIED: datetime(2026, 10, 21),
                    }
                },
                None,
            ),
            # the URL is written differently (same key)
            ({"$set": {"url": "HTTPS://beacon.example.org/api/"}}, None),
            # the verification is renewed, or removed
            ({"$set": {VERIFIED: datetime(2026, 10, 21)}}, datetime(2026, 10, 21)),
            ({"$unset": {VERIFIED: ""}}, None),
        ],
    )
    def test_concurrent_change(self, monkeypatch, unit_app, services, concurrent, kept):
        """Services changed after they were read are read again, so that the
        verification as of the replacement is kept or removed.
        """
        self._verified(services)
        find_one = services.find_one
        reads = []

        def change_after_first_read(filter, projection=None, *args, **kwargs):
            # the service changes concurrently after it is first read for the
            # replacement (not for the check for duplicates)
            result = find_one(filter, projection, *args, **kwargs)
            if projection is not None and VERIFIED in projection:
                reads.append(result)
                if len(reads) == 1:
                    services.update_one({"id": MOCK_ID}, concurrent)
            return result

        new_version = {**MOCK_SERVICE, "version": "2.0.0"}
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(new_version), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "find_one", change_after_first_read)
            obj.register_metadata()
        assert len(reads) >= 2
        stored = find_one({"id": MOCK_ID})
        assert stored["version"] == "2.0.0"
        assert stored[KEY] == service_key(new_version)
        assert stored.get(VERIFIED) == kept

    def test_changed_on_each_try(self, monkeypatch, unit_app, services):
        """If the service changes concurrently on each try, replacing it
        fails.
        """
        self._verified(services)
        find_one = services.find_one
        renewals = iter(range(1, 10))

        def change_after_read(*args, **kwargs):
            result = find_one(*args, **kwargs)
            time = datetime(2026, 10, 21, 0, 0, next(renewals))
            services.update_one({"id": MOCK_ID}, {"$set": {VERIFIED: time}})
            return result

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "find_one", change_after_read)
            with pytest.raises(InternalServerError):
                obj.register_metadata(retries=2)
