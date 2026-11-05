"""Tests for importing service info, with the app created from its config."""

from collections.abc import Callable, Iterator
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
from types import SimpleNamespace
from typing import Any

from flask.testing import FlaskClient
from foca import get_custom_config
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError
import pytest
from werkzeug.test import TestResponse

from cloud_registry.ga4gh.registry import service_import
from cloud_registry.ga4gh.registry.service import InvalidService
from cloud_registry.ga4gh.registry.service_import import FETCH_FAILED, FetchLimit
from cloud_registry.safe_fetch import AddressNotAllowed, FetchError, Unreachable
from cloud_registry.service_models.custom_config import (
    CustomConfig,
    ServiceInfoConfig,
)
from cloud_registry.utils import now
from tests.conftest import _CONNECT, ADMIN, USER, make_headers
from tests.mock_data import ERROR_MEDIA_TYPE

BASE = "/ga4gh/registry/v1"
ENDPOINT = f"{BASE}/services/import"
URL = "https://tes.example.org/ga4gh/tes/v1"
ADDRESS = f"{URL}/service-info"
INFO: dict[str, Any] = {
    "id": "org.example.tes",
    "name": "My TES",
    "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"},
    "description": "A TES.",
    "organization": {"name": "Example", "url": "https://example.org"},
    "contactUrl": "mailto:support@example.org",
    "documentationUrl": "https://docs.example.org",
    "environment": "prod",
    "version": "1.0.0",
}
EXPECTED = {key: value for key, value in INFO.items() if key != "id"} | {"url": URL}


def post(
    client: FlaskClient, address: object = ADDRESS, user: str | None = USER
) -> TestResponse:
    """Import service info.

    Args:
        client: Test client of the app.
        address: Address of the service info.
        user: User who sends the request; no token if `None`.

    Returns:
        Response.
    """
    return client.post(
        ENDPOINT,
        json={"serviceInfoUrl": address},
        headers={} if user is None else make_headers(user),
    )


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], list[str]]:
    """Function replacing the fetch by one with a given result.

    Bytes are returned as they are, exceptions raised, other values encoded as
    JSON; the function returns the list of fetched URLs.
    """

    def set_result(result: object) -> list[str]:
        """Set the result of fetches.

        Args:
            result: Result.

        Returns:
            List of the fetched URLs, filled by the fetches.
        """
        urls: list[str] = []

        def fetch(url: str, **kwargs: Any) -> bytes:
            """Replace `safe_get()`.

            Args:
                url: URL.
                **kwargs: Other arguments, ignored.

            Returns:
                The result.

            Raises:
                FetchError: The result is an exception.
            """
            urls.append(url)
            if isinstance(result, FetchError):
                raise result
            return result if isinstance(result, bytes) else json.dumps(result).encode()

        monkeypatch.setattr(service_import, "safe_get", fetch)
        return urls

    return set_result


def test_import(client: FlaskClient, serve: Callable[[object], list[str]]) -> None:
    """Service info gives the values for a registration, without storing them."""
    urls = serve(INFO)
    response = post(client)
    assert response.status_code == 200
    assert response.json == EXPECTED
    assert urls == [ADDRESS]
    assert client.get(f"{BASE}/services").json == []


def test_import_then_register(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """The values can be registered as they are."""
    serve(INFO)
    values = post(client).json
    response = client.post(f"{BASE}/services", json=values, headers=make_headers())
    assert response.status_code == 201


def test_only_registration_fields(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Other fields are ignored, also timestamps and those of objects."""
    serve(
        {
            **INFO,
            "createdAt": "2020-01-01T00:00:00Z",
            "updatedAt": "not a date",
            "storage": ["s3://bucket"],
            "registrationRules": {"environments": []},
            "url": "https://elsewhere.example.org",
            "organization": {**INFO["organization"], "logo": "x"},
            "type": {**INFO["type"], "extra": "x"},
        }
    )
    response = post(client)
    assert response.status_code == 200
    assert response.json == EXPECTED


def test_null_fields_are_missing(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Optional fields set to `null` count as missing."""
    serve({**INFO, "description": None, "contactUrl": None})
    response = post(client)
    assert response.status_code == 200
    assert response.json is not None
    assert not {"description", "contactUrl"} & set(response.json)


def test_login_required(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Imports need a login, and fetch nothing otherwise."""
    urls = serve(INFO)
    assert post(client, user=None).status_code == 401
    assert not urls


@pytest.mark.parametrize(
    "address",
    [
        URL,
        f"{URL}/service-info/",
        f"{URL}/service-info?x=1",
        f"{URL}/service-info#x",
        f"{URL}/service-info?",
        f"{URL}/other-service-info",
        "http://tes.example.org/service-info",  # scheme not allowed
        "ftp://tes.example.org/service-info",
        "https://user:secret@tes.example.org/service-info",
        "not a url/service-info",
        " https://tes.example.org/service-info",
    ],
)
def test_invalid_address(
    client: FlaskClient, serve: Callable[[object], list[str]], address: str
) -> None:
    """Addresses of anything else than service info are rejected unfetched."""
    urls = serve(INFO)
    response = post(client, address)
    assert response.status_code == 400
    assert response.mimetype == ERROR_MEDIA_TYPE
    assert not urls


@pytest.mark.parametrize(
    "error", [AddressNotAllowed(), Unreachable()], ids=["address", "unreachable"]
)
def test_failed_fetch(
    client: FlaskClient, serve: Callable[[object], list[str]], error: FetchError
) -> None:
    """All failed fetches have the same response."""
    serve(error)
    response = post(client)
    assert response.status_code == 502
    assert response.mimetype == ERROR_MEDIA_TYPE
    assert response.json == {
        "title": "Bad gateway",
        "status": 502,
        "detail": FETCH_FAILED,
    }


@pytest.mark.parametrize(
    "body",
    [
        b"<html></html>",
        b"\xff\xfe",
        b"[" * 100000,
        b"[]",
        b'"text"',
        b"null",
        b"{}",
    ],
)
def test_not_service_info(
    client: FlaskClient, serve: Callable[[object], list[str]], body: bytes
) -> None:
    """Responses that aren't service info are invalid."""
    serve(body)
    assert post(client).status_code == 400


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"id": None}, "id"),
        ({"id": 1}, "id"),
        ({"id": ""}, "id"),
        ({"id": "x" * 1025}, "id"),
        ({"name": None}, "name"),
        ({"name": 1}, "name"),
        ({"name": "x" * 1025}, "name"),
        ({"description": "x" * 4097}, "description"),
        ({"version": None}, "version"),
        ({"type": None}, "type"),
        ({"type": "tes"}, "type"),
        ({"type": {"group": "g", "artifact": "a"}}, "type.version"),
        (
            {"type": {"group": "g", "artifact": "a", "version": "x" * 257}},
            "type.version",
        ),
        ({"organization": None}, "organization"),
        ({"organization": {"name": "n"}}, "organization.url"),
        ({"organization": {"name": "n", "url": "ftp://x.org"}}, "organization.url"),
        ({"documentationUrl": "javascript:alert(1)"}, "documentationUrl"),
        ({"contactUrl": "ftp://example.org"}, "contactUrl"),
        ({"environment": "other"}, "environment"),
    ],
)
def test_invalid_values(
    client: FlaskClient,
    serve: Callable[[object], list[str]],
    change: dict[str, Any],
    field: str,
) -> None:
    """Service info is checked as a registration; errors name the field."""
    serve({**INFO, **change})
    response = post(client)
    assert response.status_code == 400
    assert response.json is not None
    assert field in response.json["detail"]


def test_error_has_no_raw_values(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Errors of the schema don't repeat what the address served."""
    serve({**INFO, "name": ["secret-internal-value"]})
    response = post(client)
    assert response.status_code == 400
    assert "secret-internal-value" not in response.text


def test_surrogates(client: FlaskClient, serve: Callable[[object], list[str]]) -> None:
    """Strings that can't be stored are invalid."""
    serve(b'{"id": "x", "name": "\\ud800"}')
    assert post(client).status_code == 400
    serve(
        json.dumps({**INFO, "name": "\ud800"}).encode(),
    )
    assert post(client).status_code == 400


def test_international_host(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Hosts are fetched encoded, and the URL stays as written."""
    urls = serve(INFO)
    response = post(client, "https://bücher.example.org/service-info")
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["url"] == "https://bücher.example.org"
    assert urls == ["https://xn--bcher-kva.example.org/service-info"]


def _limit(client: FlaskClient, limit: int) -> None:
    """Set the limit of fetches.

    Args:
        client: Test client of the app.
        limit: Fetches per hour.
    """
    with client.application.app_context():
        services = get_custom_config(CustomConfig).endpoints.services
        services.service_info.fetches_per_hour = limit


def _fetches(client: FlaskClient) -> Collection[dict[str, Any]]:
    """Get the collection of the fetches.

    Args:
        client: Test client of the app.

    Returns:
        Collection.
    """
    with client.application.app_context():
        return service_import.get_collection(service_import.FETCHES)


def test_rate_limit(client: FlaskClient, serve: Callable[[object], list[str]]) -> None:
    """Users are limited, each on their own, also for failed fetches."""
    _limit(client, 2)
    urls = serve(Unreachable())
    assert post(client).status_code == 502
    serve(INFO)
    assert post(client).status_code == 200
    urls = serve(INFO)
    response = post(client)
    assert response.status_code == 429
    assert response.mimetype == ERROR_MEDIA_TYPE
    assert 0 < int(response.headers["Retry-After"]) <= 3600
    assert not urls
    assert post(client, user="bob@example.org").status_code == 200
    # admins too: fetches are as expensive
    _limit(client, 1)
    assert post(client, user=ADMIN).status_code == 200
    assert post(client, user=ADMIN).status_code == 429


def test_invalid_address_not_counted(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Rejected addresses don't count for the limit."""
    _limit(client, 1)
    serve(INFO)
    assert post(client, URL).status_code == 400
    assert post(client).status_code == 200


def test_rate_limit_window(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """The limit starts anew when its window is over."""
    _limit(client, 1)
    serve(INFO)
    assert post(client).status_code == 200
    assert post(client).status_code == 429
    _fetches(client).update_one(
        {"_id": USER},
        {"$set": {"windowStart": now() - timedelta(hours=1, seconds=1)}},
    )
    assert post(client).status_code == 200
    assert _fetches(client).find_one({"_id": USER})["count"] == 1  # type: ignore[index]


def test_invalid_service_url(client: FlaskClient) -> None:
    """Failed checks of the service URL name it, without repeating it."""
    with client.application.app_context(), pytest.raises(InvalidService) as exc:
        service_import.service_values(INFO, "ftp://secret.example.org")
    assert exc.value.description == "'url' is missing or invalid."


def test_error_of_links_has_no_raw_values(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Errors of the checks of links don't repeat what the address served."""
    serve({**INFO, "documentationUrl": "https://abc%41.example<script>/"})
    response = post(client)
    assert response.status_code == 400
    assert response.json is not None
    assert "documentationUrl" in response.json["detail"]
    assert "script" not in response.text


@pytest.mark.parametrize(
    "address",
    [
        "https://alice:hunter2@tes.example.org/service-info",
        "https://tes.example.org/service-info?token=hunter2",
    ],
)
def test_rejected_address_not_logged(
    client: FlaskClient,
    serve: Callable[[object], list[str]],
    caplog: pytest.LogCaptureFixture,
    address: str,
) -> None:
    """Rejected addresses aren't logged, as they can hold secrets."""
    serve(INFO)
    caplog.set_level(logging.INFO, logger=service_import.__name__)
    assert post(client, address).status_code == 400
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == service_import.__name__
    ]
    assert messages
    assert not any("hunter2" in message for message in messages)


def test_concurrent_first_fetch(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the race to start a window doesn't count as being at the limit."""

    class Fetches:
        """Collection in which another request starts the window first."""

        def __init__(self) -> None:
            """Start without a document."""
            self.increments = 0

        def update_one(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            """Increment only after the insert failed.

            Args:
                query: Query.
                *args: Ignored.
                **kwargs: Ignored.

            Returns:
                Result of the increment.

            Raises:
                DuplicateKeyError: For the insert.
            """
            if kwargs.get("upsert"):
                raise DuplicateKeyError("duplicate")
            self.increments += 1
            return SimpleNamespace(modified_count=self.increments - 1)

    fetches = Fetches()
    monkeypatch.setattr(service_import, "get_collection", lambda name: fetches)
    with client.application.app_context():
        service_import.check_fetch_limit(USER)
    assert fetches.increments == 2


def test_concurrent_fetches_limited(
    client: FlaskClient, serve: Callable[[object], list[str]]
) -> None:
    """Requests can't take all threads; refused ones don't count for the limit."""
    urls = serve(INFO)
    with service_import.fetch_slot(), service_import.fetch_slot():
        response = post(client)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(service_import.BUSY_RETRY_AFTER)
        assert not urls
        assert not _fetches(client).count_documents({})
    assert post(client).status_code == 200


def test_slot_released_on_error() -> None:
    """Slots are released if the fetch fails."""
    for _ in range(service_import.MAX_CONCURRENT_FETCHES + 1):
        with pytest.raises(ValueError), service_import.fetch_slot():
            raise ValueError


def test_rate_limit_document_gone(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time to wait has a default if the count was removed meanwhile."""

    class Fetches:
        """Collection whose document is gone after the failed insert."""

        def update_one(self, query: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            """Fail like a user at the limit.

            Args:
                query: Query.
                *args: Ignored.
                **kwargs: Ignored.

            Returns:
                Result without a change, for the first attempt.

            Raises:
                DuplicateKeyError: For the insert.
            """
            if kwargs.get("upsert"):
                raise DuplicateKeyError("duplicate")
            return SimpleNamespace(modified_count=0)

        def find_one(self, *args: Any, **kwargs: Any) -> None:
            """Find nothing.

            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """

    monkeypatch.setattr(service_import, "get_collection", lambda name: Fetches())
    with client.application.app_context(), pytest.raises(FetchLimit) as exc:
        service_import.check_fetch_limit(USER)
    assert exc.value.retry_after == 3600


def test_private_networks_config() -> None:
    """Permitted networks are checked, and none are permitted by default."""
    assert ServiceInfoConfig().private_networks == []
    assert ServiceInfoConfig().fetches_per_hour == 30
    assert ServiceInfoConfig(private_networks=["10.0.0.0/8", "fd00::/8"])
    for network in ("10.0.0.1/8", "not a network"):
        with pytest.raises(ValueError):
            ServiceInfoConfig(private_networks=[network])
    with pytest.raises(ValueError):
        ServiceInfoConfig(fetches_per_hour=0)


class _Handler(BaseHTTPRequestHandler):
    """Handler of a service that serves service info."""

    requests: list[dict[str, str]] = []  # noqa: RUF012 (for the test)

    def do_GET(self) -> None:
        """Serve the service info, recording the request."""
        _Handler.requests.append({"path": self.path, **dict(self.headers.items())})
        body = json.dumps(INFO).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Stay silent.

        Args:
            format: Format of the message.
            *args: Values of the message.
        """


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Port of a local service, which the tests may connect to."""
    monkeypatch.setattr(socket.socket, "connect", _CONNECT)
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _allow_local(client: FlaskClient, networks: list[str]) -> None:
    """Allow `http`, and networks.

    Args:
        client: Test client of the app.
        networks: Networks to permit.
    """
    with client.application.app_context():
        services = get_custom_config(CustomConfig).endpoints.services
        services.url_schemes = ["http", "https"]
        services.service_info.private_networks = networks


def test_end_to_end(client: FlaskClient, port: int) -> None:
    """A local service is imported from a permitted network, without a token."""
    _allow_local(client, ["127.0.0.0/8"])
    response = post(client, f"http://127.0.0.1:{port}/api/service-info")
    assert response.status_code == 200
    assert response.json == {**EXPECTED, "url": f"http://127.0.0.1:{port}/api"}
    (sent,) = _Handler.requests
    assert sent["path"] == "/api/service-info"
    assert "Authorization" not in sent
    assert sent["User-Agent"] == "cloud-registry"


def test_end_to_end_not_permitted(client: FlaskClient, port: int) -> None:
    """Without a permitted network, a local service isn't connected to."""
    _allow_local(client, [])
    response = post(client, f"http://127.0.0.1:{port}/service-info")
    assert response.status_code == 502
    assert response.json is not None
    assert response.json["detail"] == FETCH_FAILED
    assert not _Handler.requests


def test_collection_in_config(client: FlaskClient) -> None:
    """The counts are removed by MongoDB, by an index of the config."""
    indexes = _fetches(client).index_information()
    assert any(
        index.get("expireAfterSeconds") == 7200 and index["key"] == [("windowStart", 1)]
        for index in indexes.values()
    )
