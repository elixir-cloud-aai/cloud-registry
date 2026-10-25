"""Test cases for service registration."""

from copy import deepcopy
from datetime import UTC, datetime
import logging
from typing import Any, NoReturn

from flask import Flask
from pymongo.collection import Collection
from pymongo.cursor import Cursor
from pymongo.errors import DuplicateKeyError
from pymongo.results import UpdateResult
import pytest

from cloud_registry.exceptions import InternalServerError
import cloud_registry.ga4gh.registry.service
from cloud_registry.ga4gh.registry.service import (
    CREATED,
    KEY,
    UPDATED,
    VERIFIED,
    DuplicateService,
    InvalidService,
    RegisterService,
    add_service_keys,
    check_links,
    check_strings,
    normalize_url,
    service_key,
    warn_invalid_urls,
    warn_url_schemes,
)
from cloud_registry.utils import get_custom_config
from tests.mock_data import (
    MOCK_ID,
    MOCK_ID_ONE_CHAR,
    MOCK_SERVICE,
    MOCK_TYPE,
)

# Time of registrations and updates in the tests (cf. `_fixed_clock()`)
NOW = datetime(2026, 10, 22, 9, 30, 15, 250000, tzinfo=UTC)
FORMATTED_NOW = "2026-10-22T09:30:15.250Z"
# Timestamp of a service, e.g., one an admin imports
IMPORTED = "2019-06-04T12:58:19Z"
# Timestamps sent with a service
SENT = {CREATED: IMPORTED, UPDATED: IMPORTED}


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let all registrations and updates of services happen at `NOW`."""
    monkeypatch.setattr(cloud_registry.ga4gh.registry.service, "now", lambda: NOW)


def _without_db_id(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Remove the database's `_id` field from a stored document.

    Args:
        doc: Stored document; must not be `None`.

    Returns:
        The document, without `_id`.
    """
    assert doc is not None
    del doc["_id"]
    return doc


def _check_link(url: object, field: str) -> None:
    """Check a link, in metadata with only that link (cf. `check_links()`).

    Args:
        url: URL of the link.
        field: Field of the link, as a path of keys separated by dots (e.g.,
            `organization.url`).
    """
    data: dict[str, Any] = {}
    parent = data
    *keys, last = field.split(".")
    for key in keys:
        parent = parent.setdefault(key, {})
    parent[last] = url
    check_links(data)


def _find(services: Collection[dict[str, Any]], id: str) -> dict[str, Any]:
    """Get a stored service.

    Args:
        services: Collection of services.
        id: Identifier of the service, which must be stored.

    Returns:
        The stored service.
    """
    doc = services.find_one({"id": id})
    assert doc is not None
    return doc


def _stored(data: dict[str, Any], id: str, created: bool = True) -> dict[str, Any]:
    """Get the expected document of a stored service.

    Args:
        data: Service as registered.
        id: Identifier of the service.
        created: Whether the service was registered by the test (with
            `createdAt`), not stored before without it.

    Returns:
        The service with its identifier, key and timestamps (at `NOW`).
    """
    timestamps = {CREATED: FORMATTED_NOW} if created else {}
    return {
        **data,
        **timestamps,
        UPDATED: FORMATTED_NOW,
        "id": id,
        KEY: service_key(data),
    }


class TestRegisterService:
    """Tests for `RegisterService` class."""

    @pytest.mark.parametrize("service_id, replace", [(None, False), (MOCK_ID, True)])
    def test_init(self, unit_app: Flask, service_id: str | None, replace: bool) -> None:
        """Test for constructing class, with and without an identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=service_id)
            assert obj.data["name"] == MOCK_SERVICE["name"]
            assert obj.data["id"] == service_id
            assert obj.replace is replace

    def test_register_metadata(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Test for registering a service with a randomly assigned identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert isinstance(obj.data["id"], str)
        stored = _without_db_id(services.find_one({"id": obj.data["id"]}))
        assert stored == _stored(MOCK_SERVICE, obj.data["id"])

    def test_register_metadata_literal_id_charset(self, unit_app: Flask) -> None:
        """Test for registering a service with a random identifier.

        The identifier is generated from a literal character set.
        """
        with unit_app.app_context():
            id_conf = get_custom_config().endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID_ONE_CHAR

    def test_register_metadata_dot_ids(
        self, monkeypatch: pytest.MonkeyPatch, unit_app: Flask
    ) -> None:
        """Generated identifiers `.`, `..` and `types` are replaced."""
        ids = iter([".", "..", "types", "A"])
        monkeypatch.setattr(
            "cloud_registry.ga4gh.registry.service.generate_id",
            lambda **kwargs: next(ids),
        )
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == "A"

    def test_register_metadata_with_id(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Test for registering a service with a user-supplied identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(MOCK_SERVICE, MOCK_ID)
        assert services.count_documents({}) == 1

    def test_register_metadata_with_id_replace(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Test for updating an existing service."""
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID})

        data = {**MOCK_SERVICE, "name": "new name"}
        with unit_app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(data, MOCK_ID, created=False)
        assert services.count_documents({}) == 1

    def test_register_metadata_duplicate_key_new_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Test for registering a service after a duplicate key error.

        A new identifier is generated after the error.
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
        assert _find(services, "BBBBBB")["name"] == MOCK_SERVICE["name"]

    def test_register_metadata_duplicate_keys_repeated(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Test for registering a service; running out of unique identifiers."""
        with unit_app.app_context():
            id_conf = get_custom_config().endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1
        services.insert_one({"id": MOCK_ID_ONE_CHAR})

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            with pytest.raises(InternalServerError):
                obj.register_metadata()
        assert services.count_documents({}) == 1

    def test_register_metadata_does_not_modify_data(self, unit_app: Flask) -> None:
        """Test that registering a service does not modify the caller's data."""
        data = deepcopy(MOCK_SERVICE)

        with unit_app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            RegisterService(data=data, id=obj.data["id"]).register_metadata()
        assert data == MOCK_SERVICE

    @pytest.mark.parametrize("service_id", [None, MOCK_ID])
    @pytest.mark.parametrize(
        "url", ["http://beacon.example.org/api", " HTTP://beacon.example.org"]
    )
    def test_register_metadata_http_rejected(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        service_id: str | None,
        url: str,
    ) -> None:
        """Service URLs with `http` are rejected by default, saying why."""
        with unit_app.app_context():
            obj = RegisterService(data={**MOCK_SERVICE, "url": url}, id=service_id)
            with pytest.raises(InvalidService) as exc_info:
                obj.register_metadata()
        assert exc_info.value.description == (
            "Service URLs must use one of these schemes: 'https'."
        )
        assert services.count_documents({}) == 0

    def test_register_metadata_http_allowed(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Service URLs with `http` are accepted if the scheme is allowed."""
        with unit_app.app_context():
            get_custom_config().endpoints.services.url_schemes = ["http", "https"]
            for url in ("http://beacon.example.org/api", MOCK_SERVICE["url"]):
                obj = RegisterService(data={**MOCK_SERVICE, "url": url})
                obj.register_metadata()
                assert _find(services, obj.data["id"])["url"] == url

    def test_register_metadata_https_disallowed(self, unit_app: Flask) -> None:
        """Only the allowed schemes are accepted, e.g., `http` only."""
        with unit_app.app_context():
            get_custom_config().endpoints.services.url_schemes = ["http"]
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            with pytest.raises(InvalidService, match=r"one of these schemes: 'http'\."):
                obj.register_metadata()


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
        # zero width non-joiner and joiner (IDNA 2008, CONTEXTJ)
        ("https://\u0628\u06cc\u200c\u062a.ir", "https://xn--ngbe99b612f.ir"),
        ("https://\u0915\u094d\u200d\u0937.in", "https://xn--11b2ezcw70k.in"),
        ("https://a.com。/x", "https://a.com/x"),
        ("https://my_host.internal", "https://my_host.internal"),
        ("https://[0::FFFF:1.2.3.4]/v1", "https://[::ffff:1.2.3.4]/v1"),
        ("https://[fe80::1%25eth0]", "https://[fe80::1%25eth0]"),
        ("https://tes.example.org/ü", "https://tes.example.org/%C3%BC"),
        ("http://tes.example.org:443", "http://tes.example.org:443"),
    ],
)
def test_normalize_url(url: str, normalized: str) -> None:
    """URLs are compared in a normalized form.

    With lowercase schemes and hosts, encoded host names, and without
    trailing dots of hosts, default ports, dot segments, repeated and trailing
    slashes, needless percent-encoding, queries and fragments.
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
        "https://tes.example.org:0",
        "https://tes.exa\nmple.org",
        "ht\ttps://tes.example.org",
        "\u00a0https://tes.example.org",
        "https://tes.example.org/v1\n",
        "https://tes.ex\u00adample.org",
        "https://tes\u202e.example.org",
        "https://tes.example.org/\u200dv1",
        "http://[::1/v1",
        "https://.",
        "https://[::g]",
        f"https://{'ü' * 64}.example.org",
        "https://tes example.org",
        "https://tes<x>.example.org",
        "https://tes%zz.example.org",
        "https://tes.example.org/a%ZZ",
        "https://tes.example.org/a%2",
        "https://tes.example.org/v1?a=%zz",
        "https://tes.example.org/v1#%2",
        "https://tes.example.org/\ud800",
        "https://[fe80::1%25eth\u200d0]/v1",
        "https://[fe80::1%25eth\u00fc]/v1",
        "https://[fe80::1%25]/v1",
        "https://[fe80::1%eth0]/v1",
        "https://[fe80::1%]/v1",
        # rejected since FOCA's URL helpers are used
        "https://127.1",
        "https://a..example.org",
        "https://ex%61mple.org",
        "https://tes.example.org/a|b",
    ],
)
def test_normalize_url_invalid(url: str) -> None:
    """Invalid URLs are rejected.

    Also URLs without scheme or host, with schemes other than `http` and
    `https`, user info, invalid ports or hosts, whitespace other than
    surrounding spaces, or control characters.
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
        "https://\u0628\u06cc\u200c\u062a.ir",
        "https://\u0915\u094d\u200d\u0937.in",
    ],
)
def test_check_link(url: str) -> None:
    """HTTP(S) URLs with valid hosts are valid links."""
    _check_link(url, field="documentationUrl")


@pytest.mark.parametrize(
    "url, detail",
    [
        (1, "must be strings"),
        ("https://www.example.org/a b", "must not contain whitespace"),
        (" https://www.example.org", "must not contain whitespace"),
        ("\u00a0https://www.example.org", "must not contain whitespace"),
        ("ht\ttps://www.example.org", "must not contain whitespace"),
        ("https://www.exa\nmple.org", "must not contain whitespace"),
        ("https://www.example.org/\x00", "control or format characters"),
        ("https://www.example.org/\u202ev1", "control or format characters"),
        ("https://www.ex\u00adample.org", "control or format characters"),
        ("https://www.example.org/?q=\u200c", "control or format characters"),
        ("https://www.example.org/#\u2028", "control or format characters"),
        ("https://www\u202e.example.org", "control or format characters"),
        ("https://www.example.org/\ud800", "control or format characters"),
        ("https://www.example.org/a%zz", "malformed percent-encodings"),
        ("https://www.example.org/?q=%", "malformed percent-encodings"),
        ("https://www.example.org/#%4", "malformed percent-encodings"),
        ("https://[fe80::1%25eth\u200c0]/docs", "need a valid host"),
        ("https://www.example.org:port", "must be valid URLs"),
        ("http://[::1/docs", "must be valid URLs"),
        ("https://www.example.org:0", "need a valid port"),
        ("javascript:alert(1)", "must use one of these schemes"),
        ("file:///etc/passwd", "must use one of these schemes"),
        ("ftp://ftp.example.org/pub", "must use one of these schemes"),
        ("mailto:support@example.org", "must use one of these schemes"),
        ("www.example.org/docs", "need a scheme and a host"),
        ("not a URL", "must not contain whitespace"),
        ("https:///docs", "need a scheme and a host"),
        ("https://user@www.example.org", "must not contain user info"),
        ("https://www<x>.example.org", "need a valid host"),
        ("https://www.example.org/<x>", "do not allow unencoded"),
        ("https://0x7f.0.0.1/docs", "need a valid host"),
    ],
)
def test_check_link_invalid(url: object, detail: str) -> None:
    """Invalid links are rejected, saying why.

    Links must be HTTP(S) URLs with a valid host and port, and without user
    info, whitespace, control or format characters.
    """
    with pytest.raises(InvalidService) as exc_info:
        _check_link(url, field="documentationUrl")
    assert detail in exc_info.value.description
    assert "'documentationUrl'" in exc_info.value.description


@pytest.mark.parametrize(
    "url",
    [
        "mailto:support@example.org",
        "MAILTO:support@example.org",
        "mailto:a@example.org,b@bücher.example.org?subject=TES%20access",
        "mailto:support%40tes@example.org",
        f"mailto:a@{'x' * 63}.example.org",
        "mailto:a@x-1.EXAMPLE.org",
    ],
)
def test_check_link_schemes(url: str) -> None:
    """Links can have other allowed schemes, with a path but no authority."""
    _check_link(url, field="contactUrl")


@pytest.mark.parametrize(
    "url, detail",
    [
        ("mailto:", "need valid email addresses"),
        ("mailto:support", "need valid email addresses"),
        ("mailto:@example.org", "need valid email addresses"),
        ("mailto:support@", "need valid email addresses"),
        ("mailto:a@example.org,", "need valid email addresses"),
        ("mailto:support@exa<mple.org", "do not allow unencoded"),
        ("mailto:a@b@example.org", "need valid email addresses"),
        ("mailto:a@x'onclick=alert(1)", "with a domain name"),
        ("mailto:a@-x.org", "with a domain name"),
        ("mailto:a@x-.org", "with a domain name"),
        ("mailto:a@%FF", "with a domain name"),
        ("mailto:a@::1%25eth0", "with a domain name"),
        ("mailto:a@.", "with a domain name"),
        ("mailto:a@example.org.", "with a domain name"),
        ("mailto:a@localhost", "with a domain name"),
        ("mailto:a@192.0.2.1", "with a domain name"),
        ("mailto:a@[192.0.2.1]", "do not allow unencoded"),
        (f"mailto:a@{'x' * 64}.org", "with a domain name"),
        (f"mailto:a@{'x.' * 127}org", "with a domain name"),
        ("mailto:a@b\u00fc\u0301\u0300--.org", "with a domain name"),
        ("mailto:support@example.org?subject=%zz", "malformed percent-encodings"),
        ("mailto:support%@example.org", "malformed percent-encodings"),
        ("mailto:support@example.org\ud800", "control or format characters"),
        ("mailto:#support", "need valid email addresses"),
        ("mailto:?to=support@example.org", "need valid email addresses"),
        ("mailto:support\u00ad@example.org", "control or format characters"),
        ("mailto://example.org/support", "need valid email addresses"),
        ("mailto:support@example.org\n", "must not contain whitespace"),
        ("mailto:a b@example.org", "must not contain whitespace"),
    ],
)
def test_check_link_schemes_invalid(url: str, detail: str) -> None:
    """Links with other allowed schemes need a path, but no authority.

    They must not contain whitespace or control characters either.
    """
    with pytest.raises(InvalidService) as exc_info:
        _check_link(url, field="contactUrl")
    assert exc_info.value.description.startswith("URLs in 'contactUrl' ")
    assert detail in exc_info.value.description


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
def test_check_links(data: dict[str, Any]) -> None:
    """Missing links are not checked, and valid links are accepted."""
    check_links(data)


@pytest.mark.parametrize(
    "data, field",
    [
        ({"contactUrl": "javascript:alert(1)"}, "contactUrl"),
        ({"documentationUrl": "mailto:support@example.org"}, "documentationUrl"),
        ({"organization": {"url": "file:///etc/passwd"}}, "organization.url"),
    ],
)
def test_check_links_invalid(data: dict[str, Any], field: str) -> None:
    """Invalid links are rejected, naming their field."""
    with pytest.raises(InvalidService) as exc_info:
        check_links(data)
    assert f"'{field}'" in exc_info.value.description


@pytest.mark.parametrize(
    "value", [{}, {"a": ["b", {"c": "d"}]}, "\U0001f600", {"\u00fc": 1}]
)
def test_check_strings(value: object) -> None:
    """Strings without unpaired surrogates, at any depth, are accepted."""
    check_strings(value)


@pytest.mark.parametrize(
    "value, field",
    [
        ("\ud800", "The request body"),
        ({"name": "x\udfff"}, "'name'"),
        ({"type": {"group": "\ud800"}}, "'type.group'"),
        ({"type": {"\ud800": "x"}}, "'type'"),
        ({"a": [1, ["\ud800"]]}, "'a[1][0]'"),
        ({"\ud800": "x"}, "The request body"),
    ],
)
def test_check_strings_invalid(value: object, field: str) -> None:
    """Strings and keys with unpaired surrogates are rejected, naming the field."""
    with pytest.raises(InvalidService) as exc_info:
        check_strings(value)
    assert exc_info.value.description.startswith(field)
    assert "unpaired surrogates" in exc_info.value.description


def test_service_key() -> None:
    """Keys ignore the case of the type, surrounding whitespace and its version."""
    other = {
        **MOCK_SERVICE,
        "type": {"group": "ORG.GA4GH ", "artifact": "Beacon", "version": "2.0.0"},
        "url": "https://BEACON.example.org/api/",
    }
    assert service_key(other) == service_key(MOCK_SERVICE)
    wes = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "artifact": "wes"}}
    assert service_key(wes) != service_key(MOCK_SERVICE)


def test_service_key_unambiguous() -> None:
    """Types whose fields join to the same text have different keys."""
    first = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "group": "a b", "artifact": "c"}}
    second = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "group": "a", "artifact": "b c"}}
    assert service_key(first) != service_key(second)


def test_service_key_casefold() -> None:
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
def test_service_key_invalid(data: dict[str, Any]) -> None:
    """Services need a URL and a type, as strings."""
    with pytest.raises(InvalidService):
        service_key(data)


class TestDuplicates:
    """Tests for rejecting services with the same URL and API type."""

    def _register(
        self,
        unit_app: Flask,
        data: dict[str, Any] = MOCK_SERVICE,
        id: str | None = None,
    ) -> str:
        """Register a service.

        Args:
            unit_app: App of the unit tests.
            data: Service to register.
            id: Identifier of the service; generated if `None`.

        Returns:
            Identifier of the registered service.
        """
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(data), id=id)
            obj.register_metadata()
        service_id: str = obj.data["id"]
        return service_id

    def test_post(self, unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
        """Registering a service again fails, naming the registered one."""
        existing = self._register(unit_app)
        variant = {**MOCK_SERVICE, "url": "HTTPS://Beacon.Example.org:443/api/"}
        with pytest.raises(DuplicateService) as exc_info:
            self._register(unit_app, data=variant)
        assert exc_info.value.existing_id == existing
        assert existing in exc_info.value.description
        assert services.count_documents({}) == 1

    def test_put(self, unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
        """Services can't be changed to the URL and type of another one."""
        self._register(unit_app)
        other = {**MOCK_SERVICE, "url": "https://other.example.org"}
        self._register(unit_app, data=other, id=MOCK_ID)
        with pytest.raises(DuplicateService):
            self._register(unit_app, id=MOCK_ID)
        assert _find(services, MOCK_ID)["url"] == other["url"]

    def test_put_same_service(self, unit_app: Flask) -> None:
        """Services can be replaced with the same URL and type.

        Also with a new API version.
        """
        existing = self._register(unit_app)
        new_version = {**MOCK_SERVICE, "type": {**MOCK_TYPE, "version": "2.0.0"}}
        assert self._register(unit_app, data=new_version, id=existing) == existing

    @pytest.mark.parametrize("id", [None, MOCK_ID])
    def test_concurrent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        id: str | None,
    ) -> None:
        """Services registered concurrently, after the check, are named."""
        check = RegisterService._check_duplicate
        calls: list[str] = []

        def check_later(self: RegisterService, key: str) -> None:
            """Check for duplicates, except on the first call."""
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

    def test_put_added_concurrently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Services added concurrently with the same identifier are replaced.

        I.e., on duplicate key errors not caused by another service with the
        same key, the service is read again and replaced.
        """
        replace_one = services.replace_one
        calls: list[bool] = []

        def add_first(*args: Any, **kwargs: Any) -> UpdateResult:
            """Replace the service, adding it concurrently on the first call."""
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
        assert _find(services, MOCK_ID)["name"] == "new"
        assert services.count_documents({}) == 1

    def test_put_duplicate_key_error_on_each_try(
        self, monkeypatch: pytest.MonkeyPatch, unit_app: Flask
    ) -> None:
        """Registering fails if duplicate key errors persist.

        I.e., duplicate key errors not caused by another service with the same
        key.
        """

        def fail(*args: Any, **kwargs: Any) -> NoReturn:
            """Fail with a duplicate key error."""
            raise DuplicateKeyError("id")

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "replace_one", fail)
            with pytest.raises(InternalServerError):
                obj.register_metadata(retries=2)


class TestAddServiceKeys:
    """Tests for adding keys to services registered without one."""

    def test_add(self, unit_app: Flask, services: Collection[dict[str, Any]]) -> None:
        """Keys are added to services without one."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD"})
        existing = {**MOCK_SERVICE, "id": "NEW", "url": "https://new.example.org"}
        services.insert_one({**existing, KEY: "kept"})
        with unit_app.app_context():
            add_service_keys()
        assert _find(services, "OLD")[KEY] == service_key(MOCK_SERVICE)
        assert _find(services, "NEW")[KEY] == "kept"

    def test_concurrent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Services registered concurrently with the same key are named."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD"})

        def fail(*args: Any, **kwargs: Any) -> NoReturn:
            """Fail with a duplicate key error."""
            raise DuplicateKeyError(KEY)

        monkeypatch.setattr(services, "update_one", fail)
        with (
            unit_app.app_context(),
            pytest.raises(RuntimeError, match="'OLD': same URL and API type"),
        ):
            add_service_keys()

    def test_replaced_concurrently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Keys written concurrently, e.g., by a replacement, are kept."""
        services.insert_one({**MOCK_SERVICE, "id": "OLD", KEY: "newer"})
        find = services.find

        def find_without_key(
            filter: Any = None, *args: Any, **kwargs: Any
        ) -> Cursor[dict[str, Any]]:
            """Find services, serving the service without its key."""
            # serve the service as read before its replacement
            if filter == {KEY: {"$exists": False}}:
                return find({}, {"_id": False, KEY: False})
            return find(filter, *args, **kwargs)

        monkeypatch.setattr(services, "find", find_without_key)
        with unit_app.app_context():
            add_service_keys()
        assert _find(services, "OLD")[KEY] == "newer"

    def test_added_concurrently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Keys added concurrently are not reported as duplicates.

        E.g., keys added by another worker starting at the same time.
        """
        key = service_key(MOCK_SERVICE)
        services.insert_one({**MOCK_SERVICE, "id": "OLD", KEY: key})
        find = services.find

        def find_without_key(
            filter: Any = None, *args: Any, **kwargs: Any
        ) -> Cursor[dict[str, Any]]:
            """Find services, serving the service without its key."""
            # serve the service as read before the key was added
            if filter == {KEY: {"$exists": False}}:
                return find({}, {"_id": False, KEY: False})
            return find(filter, *args, **kwargs)

        monkeypatch.setattr(services, "find", find_without_key)
        with unit_app.app_context():
            add_service_keys()
        assert _find(services, "OLD")[KEY] == key

    def test_problems(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """All services that are duplicates or have invalid URLs are named.

        I.e., services with the same URL and API type as another one, or with
        invalid URLs.
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


class TestWarnUrlSchemes:
    """Tests for warning of stored services with schemes not allowed."""

    def test_warned(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Stored services with schemes not allowed are named, others not."""
        for id, url in [
            ("HTTP", "http://beacon.example.org"),
            ("UPPER", " HTTP://other.example.org"),
            ("HTTPS", "https://third.example.org"),
            ("BAD", 1),
            ("INVALID", "http://[::1/v1"),
            ("NOHOST", "http://"),
        ]:
            services.insert_one({**MOCK_SERVICE, "id": id, "url": url})
        services.insert_one({"id": "NOURL"})
        with unit_app.app_context(), caplog.at_level(logging.WARNING):
            warn_url_schemes()
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert "'HTTP', 'UPPER'" in message
        assert "url_schemes" in message
        assert "HTTPS" not in message
        assert "BAD" not in message
        assert "INVALID" not in message
        assert "NOHOST" not in message

    def test_none(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Without such services, nothing is logged.

        Also not for `http` URLs if `http` is allowed.
        """
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID})
        with unit_app.app_context(), caplog.at_level(logging.WARNING):
            warn_url_schemes()
            services.insert_one(
                {**MOCK_SERVICE, "id": "HTTP", "url": "http://beacon.example.org"}
            )
            get_custom_config().endpoints.services.url_schemes = ["http", "https"]
            warn_url_schemes()
        assert not caplog.records


class TestWarnInvalidUrls:
    """Tests for warning of stored services with keys and invalid URLs."""

    def test_warned(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Services with keys and invalid URLs are named, others not.

        E.g., URLs accepted before stricter checks; services without keys are
        checked when their keys are added.
        """
        for id, url in [
            ("PIPE", "https://beacon.example.org/a|b"),
            ("SHORT", "https://127.1"),
            ("NOSTR", 1),
            ("VALID", " https://third.example.org"),
            ("HTTP", "http://fourth.example.org"),
        ]:
            services.insert_one({**MOCK_SERVICE, "id": id, "url": url, KEY: id})
        services.insert_one({**MOCK_SERVICE, "id": "NOKEY", "url": "https://1.1"})
        with unit_app.app_context(), caplog.at_level(logging.WARNING):
            warn_invalid_urls()
        assert len(caplog.records) == 1
        message = caplog.records[0].getMessage()
        assert message.endswith("'NOSTR', 'PIPE', 'SHORT'")
        assert "duplicates" in message

    def test_none(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Without such services, nothing is logged."""
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID, KEY: MOCK_ID})
        with unit_app.app_context(), caplog.at_level(logging.WARNING):
            warn_invalid_urls()
        assert not caplog.records


class TestVerificationReset:
    """Tests for keeping and removing verifications on replacements."""

    TIME = datetime(2026, 10, 20, 12, 0)

    def _verified(
        self, services: Collection[dict[str, Any]], data: dict[str, Any] = MOCK_SERVICE
    ) -> None:
        """Insert a verified service, with identifier `MOCK_ID`.

        Args:
            services: Collection of services.
            data: Service to insert.
        """
        services.insert_one(
            {**data, "id": MOCK_ID, KEY: service_key(data), VERIFIED: self.TIME}
        )

    def _replace(self, unit_app: Flask, data: dict[str, Any]) -> None:
        """Replace the service with identifier `MOCK_ID`, which must exist.

        Args:
            unit_app: App of the unit tests.
            data: New service.
        """
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
    def test_kept(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        change: dict[str, Any],
    ) -> None:
        """Verifications are kept if the URL (as written) and the key are unchanged."""
        self._verified(services)
        data = {**MOCK_SERVICE, **change}
        self._replace(unit_app, data)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == {**_stored(data, MOCK_ID, created=False), VERIFIED: self.TIME}

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
    def test_removed(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        change: dict[str, Any],
    ) -> None:
        """Verifications are removed if the URL (in any way) or the key changes."""
        self._verified(services)
        data = {**MOCK_SERVICE, **change}
        self._replace(unit_app, data)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(data, MOCK_ID, created=False)

    def test_without_key(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Services without a key are replaced, without their verification.

        Cf. `add_service_keys()`.
        """
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID, VERIFIED: self.TIME})
        self._replace(unit_app, MOCK_SERVICE)
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == _stored(MOCK_SERVICE, MOCK_ID, created=False)

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
    def test_concurrent_change(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        concurrent: dict[str, Any],
        kept: datetime | None,
    ) -> None:
        """Services changed after they were read are read again.

        So the verification as of the replacement is kept or removed.
        """
        self._verified(services)
        find_one = services.find_one
        reads: list[dict[str, Any] | None] = []

        def change_after_first_read(
            filter: Any, projection: Any = None, *args: Any, **kwargs: Any
        ) -> dict[str, Any] | None:
            """Find a service, changing it after its first read."""
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
        assert stored is not None
        assert stored["version"] == "2.0.0"
        assert stored[KEY] == service_key(new_version)
        assert stored.get(VERIFIED) == kept

    def test_changed_on_each_try(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """Replacing a service fails if it changes concurrently on each try."""
        self._verified(services)
        find_one = services.find_one
        renewals = iter(range(1, 10))

        def change_after_read(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            """Find a service, changing it after each read."""
            result = find_one(*args, **kwargs)
            time = datetime(2026, 10, 21, 0, 0, next(renewals))
            services.update_one({"id": MOCK_ID}, {"$set": {VERIFIED: time}})
            return result

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "find_one", change_after_read)
            with pytest.raises(InternalServerError):
                obj.register_metadata(retries=2)


class TestTimestamps:
    """Tests for the timestamps that the registry sets."""

    def _register(
        self,
        unit_app: Flask,
        data: dict[str, Any],
        id: str | None = None,
        keep_timestamps: bool = False,
    ) -> RegisterService:
        """Register a service.

        Args:
            unit_app: App of the unit tests.
            data: Service.
            id: Identifier of the service; generated if `None`.
            keep_timestamps: Whether timestamps in `data` are kept.

        Returns:
            The registration.
        """
        with unit_app.app_context():
            obj = RegisterService(
                data=deepcopy(data), id=id, keep_timestamps=keep_timestamps
            )
            obj.register_metadata()
        return obj

    @pytest.mark.parametrize("id", [None, MOCK_ID])
    def test_set_on_registration(
        self, unit_app: Flask, services: Collection[dict[str, Any]], id: str | None
    ) -> None:
        """Both timestamps are set on registration; sent ones are ignored."""
        obj = self._register(unit_app, {**MOCK_SERVICE, **SENT}, id=id)
        stored = _find(services, obj.data["id"])
        assert (stored[CREATED], stored[UPDATED]) == (FORMATTED_NOW, FORMATTED_NOW)

    def test_ignored_unchecked(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Ignored timestamps aren't checked."""
        obj = self._register(unit_app, {**MOCK_SERVICE, CREATED: "yesterday"})
        assert _find(services, obj.data["id"])[CREATED] == FORMATTED_NOW

    @pytest.mark.parametrize("id", [None, MOCK_ID])
    def test_kept_on_registration(
        self, unit_app: Flask, services: Collection[dict[str, Any]], id: str | None
    ) -> None:
        """Sent timestamps are kept with `keep_timestamps`."""
        obj = self._register(
            unit_app, {**MOCK_SERVICE, **SENT}, id=id, keep_timestamps=True
        )
        stored = _find(services, obj.data["id"])
        assert (stored[CREATED], stored[UPDATED]) == (IMPORTED, IMPORTED)

    def test_kept_invalid(self, unit_app: Flask) -> None:
        """Kept timestamps must be RFC 3339 date-times."""
        with pytest.raises(InvalidService, match=CREATED):
            self._register(
                unit_app, {**MOCK_SERVICE, CREATED: "yesterday"}, keep_timestamps=True
            )

    @pytest.mark.parametrize(
        "sent, keep_timestamps, expected",
        [
            ({}, False, (IMPORTED, FORMATTED_NOW)),
            ({CREATED: "2020-01-01T00:00:00Z"}, False, (IMPORTED, FORMATTED_NOW)),
            ({}, True, (IMPORTED, FORMATTED_NOW)),
            (
                {CREATED: "2020-01-01T00:00:00Z"},
                True,
                ("2020-01-01T00:00:00Z", FORMATTED_NOW),
            ),
            # sent back as read, e.g., by an admin editing the service
            ({**SENT}, True, (IMPORTED, FORMATTED_NOW)),
            # only kept for new services
            ({UPDATED: "2020-01-01T00:00:00Z"}, True, (IMPORTED, FORMATTED_NOW)),
        ],
    )
    def test_update(
        self,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
        sent: dict[str, str],
        keep_timestamps: bool,
        expected: tuple[str, str],
    ) -> None:
        """Updates keep `createdAt` and set `updatedAt`, unless they are kept."""
        services.insert_one({**MOCK_SERVICE, **SENT, "id": MOCK_ID})
        self._register(
            unit_app,
            {**MOCK_SERVICE, **sent},
            id=MOCK_ID,
            keep_timestamps=keep_timestamps,
        )
        stored = _find(services, MOCK_ID)
        assert (stored[CREATED], stored[UPDATED]) == expected

    def test_update_without_created(
        self, unit_app: Flask, services: Collection[dict[str, Any]]
    ) -> None:
        """Services stored without `createdAt` are kept without it."""
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID})
        self._register(unit_app, MOCK_SERVICE, id=MOCK_ID)
        stored = _find(services, MOCK_ID)
        assert CREATED not in stored
        assert stored[UPDATED] == FORMATTED_NOW

    def test_concurrent_change(
        self,
        monkeypatch: pytest.MonkeyPatch,
        unit_app: Flask,
        services: Collection[dict[str, Any]],
    ) -> None:
        """A `createdAt` changed after the service was read is kept."""
        services.insert_one({**MOCK_SERVICE, **SENT, "id": MOCK_ID})
        find_one = services.find_one
        reads: list[dict[str, Any] | None] = []
        changed = "2020-01-01T00:00:00Z"

        def change_after_first_read(
            filter: Any, projection: Any = None, *args: Any, **kwargs: Any
        ) -> dict[str, Any] | None:
            """Find a service, changing `createdAt` after its first read."""
            result = find_one(filter, projection, *args, **kwargs)
            if projection is not None and CREATED in projection:
                reads.append(result)
                if len(reads) == 1:
                    services.update_one({"id": MOCK_ID}, {"$set": {CREATED: changed}})
            return result

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            monkeypatch.setattr(obj.db_coll, "find_one", change_after_first_read)
            obj.register_metadata()
        assert len(reads) == 2
        assert _find(services, MOCK_ID)[CREATED] == changed
