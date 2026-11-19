"""Tests for the records of failed imports, registrations, updates and syncs."""

from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
import json
import logging
from typing import Any

from flask.testing import FlaskClient
from foca import get_custom_config
from foca.errors import FieldError
from pydantic import ValidationError
from pymongo.collection import Collection
import pytest

from cloud_registry.ga4gh.registry import failures, server, service_import, sync
from cloud_registry.ga4gh.registry.failures import (
    FAILURES,
    OCCURRED_AT,
    PAGE_TOKEN,
    SECONDS_PER_DAY,
    FailureReason,
    fetch_reason,
    invalid_fields,
)
from cloud_registry.ga4gh.registry.service import SYNC, InvalidService
from cloud_registry.ga4gh.registry.service_import import InvalidServiceInfo
from cloud_registry.safe_fetch import AddressNotAllowed, FetchError, Unreachable
from cloud_registry.service_models.custom_config import (
    CustomConfig,
    FailuresConfig,
)
from cloud_registry.utils import get_collection, now
from tests.conftest import ADMIN, USER, make_headers

BASE = "/ga4gh/registry/v1"
ENDPOINT = f"{BASE}/failures"
URL = "https://tes.example.org/ga4gh/tes/v1"
ADDRESS = f"{URL}/service-info"
TYPE = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": TYPE,
    "organization": {"name": "Example", "url": "https://example.org"},
    "version": "1.0.0",
    "url": URL,
}
INFO: dict[str, Any] = {
    "id": "org.example.tes",
    **{key: value for key, value in SERVICE.items() if key != "url"},
}
OTHER = "bob@example.org"
MAINTAINER = "erin@example.org"
Serve = Callable[[object], None]


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Serve:
    """Function replacing fetches by ones with a given result.

    Exceptions are raised, bytes returned as they are, other values encoded as
    JSON.
    """

    def set_result(result: object) -> None:
        """Set the result of fetches.

        Args:
            result: Result.
        """

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
            if isinstance(result, FetchError):
                raise result
            if isinstance(result, bytes):
                return result
            return json.dumps(result).encode()

        monkeypatch.setattr(service_import, "safe_get", fetch)

    return set_result


@pytest.fixture(autouse=True)
def no_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let users sync a service as often as they like."""
    monkeypatch.setattr(sync, "SYNC_REQUEST_GAP", timedelta(0))


def list_failures(
    client: FlaskClient,
    user: str = USER,
    admin: bool = False,
    **query: Any,
) -> dict[str, Any]:
    """List the failures that a user may see.

    Args:
        client: Test client of the app.
        user: User.
        admin: Whether the request asks for admin mode.
        **query: Query parameters.

    Returns:
        Page of failures.
    """
    response = client.get(
        ENDPOINT,
        query_string=query,
        headers=make_headers(user, roles="admin" if admin else None),
    )
    assert response.status_code == 200, response.json
    assert isinstance(response.json, dict)
    return response.json


def reasons(page: dict[str, Any]) -> list[str]:
    """Get the reasons of the failures of a page.

    Args:
        page: Page of failures.

    Returns:
        Reasons, in the order of the page.
    """
    return [item["reason"] for item in page["failures"]]


def register(
    client: FlaskClient, service: dict[str, Any] | None = None, user: str = USER
) -> str:
    """Register a service.

    Args:
        client: Test client of the app.
        service: Service; the test service if `None`.
        user: User who registers it.

    Returns:
        Identifier of the service.
    """
    response = client.post(
        f"{BASE}/services", json=service or SERVICE, headers=make_headers(user)
    )
    assert response.status_code == 201
    assert isinstance(response.json, str)
    return response.json


def add_maintainer(client: FlaskClient, service_id: str) -> None:
    """Make `MAINTAINER` a maintainer of a service of `USER`.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
    """
    response = client.put(
        f"{BASE}/services/{service_id}/permissions/users/{MAINTAINER}",
        json={"role": "maintainer"},
        headers=make_headers(),
    )
    assert response.status_code == 200


def stored(client: FlaskClient) -> Collection[dict[str, Any]]:
    """Get the collection of the failures.

    Args:
        client: Test client of the app.

    Returns:
        Collection.
    """
    with client.application.app_context():
        return get_collection(FAILURES)


def settings(client: FlaskClient) -> FailuresConfig:
    """Get the settings of failures, which tests can change.

    Args:
        client: Test client of the app.

    Returns:
        Settings.
    """
    with client.application.app_context():
        return get_custom_config(CustomConfig).endpoints.services.failures


def import_info(client: FlaskClient, address: str = ADDRESS, user: str = USER) -> int:
    """Import service info.

    Args:
        client: Test client of the app.
        address: Address of the service info.
        user: User.

    Returns:
        Status code of the response.
    """
    response = client.post(
        f"{BASE}/services/import",
        json={"serviceInfoUrl": address},
        headers=make_headers(user),
    )
    return response.status_code


def run_syncs(client: FlaskClient) -> None:
    """Make all services with sync due, and sync them.

    Args:
        client: Test client of the app.
    """
    with client.application.app_context():
        get_collection("services").update_many(
            {SYNC: {"$exists": True}},
            {"$set": {sync.NEXT: now() - timedelta(seconds=1)}},
        )
        sync.run_due_syncs()


def test_requires_login(client: FlaskClient) -> None:
    """Failures are only listed to authenticated users."""
    assert client.get(ENDPOINT).status_code == 401


def test_none(client: FlaskClient) -> None:
    """Without failures, the page is empty, without a token."""
    assert list_failures(client) == {"failures": []}


def test_import_invalid_address(client: FlaskClient) -> None:
    """Invalid addresses are recorded with the field, without the address."""
    assert import_info(client, "https://user:secret@tes.example.org/x") == 400
    [item] = list_failures(client)["failures"]
    assert item["operation"] == "import"
    assert item["reason"] == "invalid field"
    assert item["fields"] == ["serviceInfoUrl"]
    assert item["userId"] == USER
    assert item["occurredAt"].endswith("Z")
    assert "url" not in item
    assert "secret" not in json.dumps(item)


@pytest.mark.parametrize(
    ("error", "reason"),
    [(Unreachable(), "unreachable"), (AddressNotAllowed(), "address not allowed")],
)
def test_import_fetch_failed(
    client: FlaskClient, serve: Serve, error: FetchError, reason: str
) -> None:
    """Failed fetches say why to admins only."""
    serve(error)
    assert import_info(client) == 502
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "fetch failed"
    assert item["url"] == URL
    [admin_item] = list_failures(client, ADMIN, admin=True)["failures"]
    assert admin_item["reason"] == reason
    assert admin_item["userId"] == USER
    # the filter by reason, as each sees it
    assert reasons(list_failures(client, reason="fetch failed")) == ["fetch failed"]
    assert list_failures(client, reason=reason)["failures"] == []
    assert reasons(list_failures(client, ADMIN, admin=True, reason=reason)) == [reason]
    assert reasons(list_failures(client, ADMIN, admin=True, reason="fetch failed")) == [
        reason
    ]


def test_import_not_json(client: FlaskClient, serve: Serve) -> None:
    """Responses that aren't JSON are recorded as such."""
    serve(b"<html>")
    assert import_info(client) == 400
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "not JSON"
    assert "fields" not in item


def test_import_invalid_service_info(client: FlaskClient, serve: Serve) -> None:
    """Invalid service info is recorded with the field, without its value."""
    serve({**INFO, "organization": {"name": "Example", "url": "ftp://secret"}})
    assert import_info(client) == 400
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "invalid service info"
    assert item["fields"] == ["organization.url"]
    assert "secret" not in json.dumps(item)


def test_import_limit_not_recorded(client: FlaskClient, serve: Serve) -> None:
    """Requests above the limit of fetches aren't recorded."""
    serve(INFO)
    with client.application.app_context():
        services = get_custom_config(CustomConfig).endpoints.services
        services.service_info.fetches_per_hour = 1
    assert import_info(client) == 200
    assert import_info(client) == 429
    assert list_failures(client)["failures"] == []


def test_register_invalid_field(client: FlaskClient) -> None:
    """Registrations with invalid fields are recorded without invalid URLs."""
    response = client.post(
        f"{BASE}/services",
        json={**SERVICE, "url": "http://tes.example.org/"},
        headers=make_headers(),
    )
    assert response.status_code == 400
    [item] = list_failures(client)["failures"]
    assert item["operation"] == "register"
    assert item["reason"] == "invalid field"
    assert item["fields"] == ["url"]
    assert "url" not in item
    assert "serviceId" not in item


def test_register_duplicate(client: FlaskClient) -> None:
    """Duplicate registrations are recorded with the URL, for the user only."""
    register(client)
    response = client.post(
        f"{BASE}/services", json=SERVICE, headers=make_headers(OTHER)
    )
    assert response.status_code == 409
    [item] = list_failures(client, OTHER)["failures"]
    assert item["reason"] == "duplicate service"
    assert item["url"] == URL
    # not a failure of the service of the owner
    assert list_failures(client)["failures"] == []


def test_register_services_limit(client: FlaskClient) -> None:
    """Registrations above the limit of services are recorded."""
    with client.application.app_context():
        get_custom_config(CustomConfig).access_control.max_services_per_user = 0
    response = client.post(f"{BASE}/services", json=SERVICE, headers=make_headers())
    assert response.status_code == 409
    assert reasons(list_failures(client)) == ["services limit reached"]


def test_put_new_service_is_register(client: FlaskClient) -> None:
    """Failed `PUT`s of new services count as registrations."""
    response = client.put(
        f"{BASE}/services/NEW",
        json={**SERVICE, "url": "http://tes.example.org/"},
        headers=make_headers(ADMIN),
    )
    assert response.status_code == 400
    [item] = list_failures(client, ADMIN, admin=True)["failures"]
    assert item["operation"] == "register"
    assert "serviceId" not in item


def test_update_seen_by_roles(client: FlaskClient) -> None:
    """Failed updates are seen by the users with a role on the service."""
    service_id = register(client)
    add_maintainer(client, service_id)
    response = client.put(
        f"{BASE}/services/{service_id}",
        json={**SERVICE, "url": "http://tes.example.org/"},
        headers=make_headers(MAINTAINER),
    )
    assert response.status_code == 400
    [item] = list_failures(client, MAINTAINER)["failures"]
    assert item["operation"] == "update"
    assert item["serviceId"] == service_id
    # the stored URL, which is valid
    assert item["url"] == URL
    assert item["userId"] == MAINTAINER
    # the owner sees it, but not who acted
    [owner_item] = list_failures(client)["failures"]
    assert "userId" not in owner_item
    assert list_failures(client, OTHER)["failures"] == []
    # admins see all only in admin mode
    assert list_failures(client, ADMIN)["failures"] == []
    [admin_item] = list_failures(client, ADMIN, admin=True)["failures"]
    assert admin_item["userId"] == MAINTAINER
    # not after the service is deleted
    deleted = client.delete(f"{BASE}/services/{service_id}", headers=make_headers())
    assert deleted.status_code == 200
    assert list_failures(client)["failures"] == []
    assert len(list_failures(client, MAINTAINER)["failures"]) == 1


def test_update_pending(client: FlaskClient) -> None:
    """Updates of pending services by users other than admins are recorded."""
    with client.application.app_context():
        get_custom_config(CustomConfig).access_control.service_types = "moderated"
    new_type = {**TYPE, "artifact": "new"}
    service_id = register(client, {**SERVICE, "type": new_type})
    response = client.put(
        f"{BASE}/services/{service_id}",
        json={**SERVICE, "type": new_type, "name": "Other"},
        headers=make_headers(),
    )
    assert response.status_code == 409
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "pending service"
    assert item["operation"] == "update"


def turn_on(client: FlaskClient, service_id: str, user: str = USER) -> int:
    """Turn sync of a service on, or sync it now.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User.

    Returns:
        Status code of the response.
    """
    response = client.put(
        f"{BASE}/services/{service_id}/sync", headers=make_headers(user, roles=None)
    )
    return response.status_code


def test_sync_on_request(client: FlaskClient, serve: Serve) -> None:
    """Failures of syncs that users start are recorded, fetches masked."""
    service_id = register(client)
    serve(Unreachable())
    assert turn_on(client, service_id) == 502
    serve(b"x")
    assert turn_on(client, service_id) == 400
    serve({**INFO, "type": {**TYPE, "artifact": "wes"}})
    assert turn_on(client, service_id) == 409
    page = list_failures(client)
    assert reasons(page) == ["identity mismatch", "not JSON", "fetch failed"]
    assert {item["operation"] for item in page["failures"]} == {"sync"}
    assert {item["serviceId"] for item in page["failures"]} == {service_id}
    assert reasons(list_failures(client, ADMIN, admin=True))[-1] == "unreachable"


def test_sync_type_not_applied(client: FlaskClient, serve: Serve) -> None:
    """Service info that can't be applied is recorded for the user who synced."""
    service_id = register(client)
    serve(INFO)
    assert turn_on(client, service_id) == 200
    with client.application.app_context():
        get_custom_config(CustomConfig).access_control.service_types = "controlled"
    serve({**INFO, "type": {**TYPE, "version": "9.9.9"}})
    assert turn_on(client, service_id) == 200
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "version not allowed"
    assert item["userId"] == USER


def test_scheduled_sync(client: FlaskClient, serve: Serve) -> None:
    """Failures of the syncs of the app say why, without a user."""
    service_id = register(client)
    add_maintainer(client, service_id)
    serve(INFO)
    assert turn_on(client, service_id) == 200
    serve(Unreachable())
    # counts as failed after the retries only
    for _ in range(sync.RETRIES):
        run_syncs(client)
        assert list_failures(client)["failures"] == []
    run_syncs(client)
    [item] = list_failures(client)["failures"]
    assert item["reason"] == "unreachable"
    assert item["serviceId"] == service_id
    assert "userId" not in item
    assert list_failures(client, MAINTAINER) == list_failures(client)
    serve({**INFO, "organization": "Example"})
    run_syncs(client)
    first = list_failures(client)["failures"][0]
    assert first["reason"] == "invalid service info"
    assert first["fields"] == ["organization"]


def test_scheduled_sync_not_written(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failures of syncs whose result isn't written aren't recorded."""
    service_id = register(client)
    serve(INFO)
    assert turn_on(client, service_id) == 200
    serve(AddressNotAllowed())
    monkeypatch.setattr(sync, "_write", lambda *args, **kwargs: False)
    run_syncs(client)
    assert list_failures(client)["failures"] == []


def test_filters(client: FlaskClient, serve: Serve) -> None:
    """Failures can be filtered by operation and service."""
    service_id = register(client)
    serve(Unreachable())
    assert turn_on(client, service_id) == 502
    assert import_info(client) == 502
    assert reasons(list_failures(client, operation="import")) == ["fetch failed"]
    page = list_failures(client, serviceId=service_id)
    assert [item["operation"] for item in page["failures"]] == ["sync"]
    assert list_failures(client, serviceId="other")["failures"] == []


def test_pages(client: FlaskClient) -> None:
    """Failures are listed in pages, most recent first."""
    for _ in range(5):
        assert import_info(client, "https://tes.example.org/") == 400
    ids = [item["id"] for item in list_failures(client, pageSize=10)["failures"]]
    assert len(ids) == 5
    assert ids == sorted(ids, reverse=True)
    first = list_failures(client, pageSize=2)
    assert [item["id"] for item in first["failures"]] == ids[:2]
    second = list_failures(client, pageSize=2, pageToken=first["nextPageToken"])
    assert [item["id"] for item in second["failures"]] == ids[2:4]
    third = list_failures(client, pageSize=2, pageToken=second["nextPageToken"])
    assert [item["id"] for item in third["failures"]] == ids[4:]
    assert "nextPageToken" not in third


@pytest.mark.parametrize(
    "query",
    [
        {"pageToken": "x"},
        # an identifier without a time
        {"pageToken": "0" * 24},
        {"pageSize": 0},
    ],
)
def test_invalid_page(client: FlaskClient, query: dict[str, Any]) -> None:
    """Invalid page parameters are rejected."""
    response = client.get(ENDPOINT, query_string=query, headers=make_headers())
    assert response.status_code == 400


@pytest.mark.parametrize(
    "token",
    [
        # `$` of the schema's pattern also matches before a final line break
        f"1.{'0' * 24}\n",
        # matches the pattern, but its time is after the year 9999
        f"{'9' * 15}.{'0' * 24}",
    ],
)
def test_malformed_page_token(client: FlaskClient, token: str) -> None:
    """Tokens that pass the schema but are malformed are rejected.

    The problem details don't quote them.
    """
    response = client.get(
        ENDPOINT, query_string={"pageToken": token}, headers=make_headers()
    )
    assert response.status_code == 400
    assert isinstance(response.json, dict)
    assert response.json["errors"] == [
        {
            "detail": "Must be the 'nextPageToken' of a page.",
            "code": "pattern-mismatch",
            "parameter": "pageToken",
        }
    ]
    body = response.get_data(as_text=True)
    assert token not in body
    assert json.dumps(token)[1:-1] not in body
    assert "0" * 24 not in body


def test_page_token_pattern(client: FlaskClient) -> None:
    """The controller checks tokens by the pattern of the specification."""
    response = client.get(f"{BASE}/openapi.json")
    assert isinstance(response.json, dict)
    (token,) = [
        param
        for param in response.json["paths"]["/failures"]["get"]["parameters"]
        if param["name"] == "pageToken"
    ]
    assert token["schema"]["pattern"] == f"^{PAGE_TOKEN.pattern}$"


def test_limit_per_user(client: FlaskClient) -> None:
    """Only so many failures of a user are recorded per hour."""
    settings(client).max_per_user_per_hour = 2
    for _ in range(3):
        assert import_info(client, "https://tes.example.org/") == 400
    assert len(list_failures(client)["failures"]) == 2
    # others are recorded, and the user's again after an hour
    assert import_info(client, "https://tes.example.org/", user=OTHER) == 400
    assert len(list_failures(client, OTHER)["failures"]) == 1
    stored(client).update_many(
        {"userId": USER}, {"$set": {OCCURRED_AT: now() - timedelta(hours=2)}}
    )
    assert import_info(client, "https://tes.example.org/") == 400
    assert len(list_failures(client)["failures"]) == 3


def test_recording_errors_logged(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Errors while recording are logged; the response stays the same."""

    def broken(*args: Any, **kwargs: Any) -> None:
        """Fail like a database that is unavailable.

        Args:
            *args: Arguments, ignored.
            **kwargs: Keyword arguments, ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(failures, "_over_limit", broken)
    with caplog.at_level(logging.ERROR, logger=failures.__name__):
        assert import_info(client, "https://tes.example.org/") == 400
    assert "Failure of operation 'import' not recorded." in caplog.text
    assert list_failures(client)["failures"] == []


def test_ttl_index(client: FlaskClient) -> None:
    """The app sets the TTL index of failures to the configured days."""
    index = stored(client).index_information()[f"{OCCURRED_AT}_1"]
    assert index["expireAfterSeconds"] == 30 * SECONDS_PER_DAY
    settings(client).keep_days = 7
    with client.application.app_context():
        failures.apply_keep_days()
    index = stored(client).index_information()[f"{OCCURRED_AT}_1"]
    assert index["expireAfterSeconds"] == 7 * SECONDS_PER_DAY


@pytest.mark.parametrize("days", [0, 366])
def test_keep_days_bounds(days: int) -> None:
    """Days outside the bounds are refused."""
    with pytest.raises(ValidationError):
        FailuresConfig(keep_days=days)


def test_fetch_reason() -> None:
    """Fetch errors other than disallowed addresses count as unreachable."""
    assert fetch_reason(AddressNotAllowed()) is FailureReason.ADDRESS_NOT_ALLOWED
    assert fetch_reason(FetchError()) is FailureReason.UNREACHABLE


def test_invalid_fields() -> None:
    """Names of invalid fields are kept only if they look like names."""
    exc = InvalidService(
        field_errors=[
            FieldError(detail="x", code="x", pointer="#/url"),
            FieldError(detail="x", code="x", pointer="#/url"),
            FieldError(detail="x", code="x", pointer="#/a%20b"),
            FieldError(detail="x", code="x", parameter="serviceId"),
        ]
    )
    assert invalid_fields(exc) == ["url", "serviceId"]
    assert invalid_fields(InvalidServiceInfo("x", field="type.group")) == ["type.group"]
    many = InvalidService(
        field_errors=[
            FieldError(detail="x", code="x", pointer=f"#/f{index}")
            for index in range(20)
        ]
    )
    assert len(invalid_fields(many)) == failures.FIELDS_PER_FAILURE


def test_unknown_service_url_not_recorded(client: FlaskClient) -> None:
    """URLs that aren't strings aren't recorded."""
    with client.application.app_context():
        failures.record_failure(
            failures.Operation.REGISTER, FailureReason.INVALID_FIELD, url=deepcopy([])
        )
    [item] = list_failures(client, ADMIN, admin=True)["failures"]
    assert "url" not in item


def test_sync_reasons_are_failure_reasons() -> None:
    """Each reason of syncs is a reason of failures, which records it."""
    assert {str(item) for item in sync.Reason} <= {str(item) for item in FailureReason}


def test_url_without_query(client: FlaskClient) -> None:
    """URLs are recorded without query and fragment, which could hold tokens."""
    response = client.post(
        f"{BASE}/services",
        json={**SERVICE, "url": f"{URL}?token=secret#x", "environment": "nope"},
        headers=make_headers(),
    )
    assert response.status_code == 400
    [item] = list_failures(client)["failures"]
    assert item["url"] == URL


def test_service_lookup_errors_logged(
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Errors while looking up the service of a failure are logged, not raised."""

    def broken(name: str) -> None:
        """Fail like a database that is unavailable.

        Args:
            name: Name of the collection, ignored.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(server, "get_collection", broken)
    with (
        client.application.app_context(),
        caplog.at_level(logging.ERROR, logger=server.__name__),
    ):
        server._record_register_failure("ABC123", FailureReason.PENDING_SERVICE)
    assert "Failure of a registration or update not recorded." in caplog.text
    assert list_failures(client, ADMIN, admin=True)["failures"] == []


def test_pages_by_time(client: FlaskClient) -> None:
    """Failures are ordered by their time, then by identifier, across pages."""
    for _ in range(3):
        assert import_info(client, "https://tes.example.org/") == 400
    first, second, third = sorted(item["_id"] for item in stored(client).find())
    current = now()
    # the newest identifier failed first, the two others at the same time
    stored(client).update_one(
        {"_id": third}, {"$set": {OCCURRED_AT: current - timedelta(seconds=1)}}
    )
    stored(client).update_many(
        {"_id": {"$in": [first, second]}}, {"$set": {OCCURRED_AT: current}}
    )
    page = list_failures(client, pageSize=1)
    ids = [page["failures"][0]["id"]]
    while "nextPageToken" in page:
        page = list_failures(client, pageSize=1, pageToken=page["nextPageToken"])
        ids += [item["id"] for item in page["failures"]]
    assert ids == [str(second), str(first), str(third)]
