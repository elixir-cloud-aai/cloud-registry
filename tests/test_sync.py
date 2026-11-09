"""Tests for syncing services with their service info, with the app itself."""

from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from typing import Any

from connexion import App
from flask.testing import FlaskClient
from foca import get_custom_config
from pymongo.collection import Collection
import pytest
from werkzeug.test import TestResponse

from cloud_registry.ga4gh.registry import service_import, sync
from cloud_registry.ga4gh.registry.service import SELF_ENTRY, SYNC, VERIFIED
from cloud_registry.safe_fetch import AddressNotAllowed, FetchError, Unreachable
from cloud_registry.service_models.custom_config import (
    MAX_SYNC_INTERVAL_HOURS,
    MIN_SYNC_INTERVAL_HOURS,
    CustomConfig,
    ServiceInfoConfig,
)
from cloud_registry.utils import get_collection, now
from tests.conftest import _CONNECT, ADMIN, USER, make_headers

BASE = "/ga4gh/registry/v1"
URL = "https://tes.example.org/ga4gh/tes/v1"
TYPE = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": TYPE,
    "description": "A TES.",
    "organization": {"name": "Example", "url": "https://example.org"},
    "version": "1.0.0",
    "url": URL,
}
INFO: dict[str, Any] = {
    "id": "org.example.tes",
    **{key: value for key, value in SERVICE.items() if key != "url"},
}
OTHER = "bob@example.org"
Serve = Callable[[object], list[str]]


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Serve:
    """Function replacing fetches by ones with a given result.

    Exceptions are raised, other values encoded as JSON; the function returns
    the list of fetched URLs.
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
            return json.dumps(result).encode()

        monkeypatch.setattr(service_import, "safe_get", fetch)
        return urls

    return set_result


def info(**fields: Any) -> dict[str, Any]:
    """Get service info with changed fields.

    Args:
        **fields: Fields to set.

    Returns:
        Service info.
    """
    return {**deepcopy(INFO), **fields}


def new_type(version: str = "1.2.0") -> dict[str, str]:
    """Get the service type with another version.

    Args:
        version: Version.

    Returns:
        Service type.
    """
    return {**TYPE, "version": version}


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


def turn_on(client: FlaskClient, service_id: str, user: str = USER) -> TestResponse:
    """Turn sync on.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        user: User who sends the request.

    Returns:
        Response.
    """
    return client.put(
        f"{BASE}/services/{service_id}/sync", headers=make_headers(user, roles=None)
    )


@pytest.fixture
def service_id(client: FlaskClient, serve: Serve) -> str:
    """Identifier of a registered service with sync on, in sync."""
    serve(INFO)
    identifier = register(client)
    assert turn_on(client, identifier).status_code == 200
    return identifier


def services(client: FlaskClient) -> Collection[dict[str, Any]]:
    """Get the collection of the services.

    Args:
        client: Test client of the app.

    Returns:
        Collection.
    """
    with client.application.app_context():
        return get_collection("services")


def run_syncs(client: FlaskClient) -> int:
    """Make all services with sync due, and sync them.

    Args:
        client: Test client of the app.

    Returns:
        Number of services claimed.
    """
    services(client).update_many(
        {SYNC: {"$exists": True}},
        {"$set": {sync.NEXT: now() - timedelta(seconds=1)}},
    )
    with client.application.app_context():
        return sync.run_due_syncs()


def state(client: FlaskClient, service_id: str) -> dict[str, Any]:
    """Get the sync state of a service, as served.

    Args:
        client: Test client of the app.
        service_id: Service identifier.

    Returns:
        Sync state.
    """
    response = client.get(f"{BASE}/services/{service_id}/sync")
    assert response.status_code == 200
    assert isinstance(response.json, dict)
    return response.json


def get_service(client: FlaskClient, service_id: str) -> dict[str, Any]:
    """Get a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.

    Returns:
        Service, as served.
    """
    response = client.get(f"{BASE}/services/{service_id}")
    assert response.status_code == 200
    assert isinstance(response.json, dict)
    return response.json


def stored_state(client: FlaskClient, service_id: str) -> dict[str, Any]:
    """Get the stored sync state of a service.

    Args:
        client: Test client of the app.
        service_id: Service identifier.

    Returns:
        Sync state.
    """
    stored = services(client).find_one({"id": service_id})
    assert stored is not None
    return stored[SYNC]


def service_info_settings(client: FlaskClient) -> ServiceInfoConfig:
    """Get the settings of service info.

    Args:
        client: Test client of the app.

    Returns:
        Settings, which tests can change.
    """
    with client.application.app_context():
        return get_custom_config(CustomConfig).endpoints.services.service_info


def set_access_control(client: FlaskClient, **settings: Any) -> None:
    """Change the settings of access control.

    Args:
        client: Test client of the app.
        **settings: Settings.
    """
    with client.application.app_context():
        access_control = get_custom_config(CustomConfig).access_control
        for name, value in settings.items():
            setattr(access_control, name, value)


def test_turn_on(client: FlaskClient, serve: Serve) -> None:
    """Sync starts with a fetch of the service's URL, and applies it."""
    urls = serve(info(description="New."))
    identifier = register(client)
    response = turn_on(client, identifier)
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["status"] == "synced"
    assert response.json["url"] == URL
    assert response.json["type"] == TYPE
    assert response.json["serviceInfoId"] == INFO["id"]
    assert response.json["lastAttemptAt"] == response.json["lastSuccessAt"]
    assert "reason" not in response.json
    assert urls == [f"{URL}/service-info"]
    assert get_service(client, identifier)["description"] == "New."
    # not served with the service
    assert not {SYNC, "sync"} & set(get_service(client, identifier))


@pytest.mark.parametrize(
    "user", [ADMIN, "erin@example.org"], ids=["admin", "maintainer"]
)
def test_who_turns_on(client: FlaskClient, serve: Serve, user: str) -> None:
    """Maintainers and admins can turn it on and off."""
    serve(INFO)
    identifier = register(client)
    assert (
        client.put(
            f"{BASE}/services/{identifier}/permissions/users/erin@example.org",
            json={"role": "maintainer"},
            headers=make_headers(),
        ).status_code
        == 200
    )
    headers = make_headers(user)
    url = f"{BASE}/services/{identifier}/sync"
    assert client.put(url, headers=headers).status_code == 200
    assert client.delete(url, headers=headers).status_code == 200


def test_others_may_not_turn_on(client: FlaskClient, service_id: str) -> None:
    """Other users can't turn it on or off, and anonymous requests need a login."""
    url = f"{BASE}/services/{service_id}/sync"
    for method in (client.put, client.delete):
        assert method(url, headers=make_headers(OTHER)).status_code == 403
        assert method(url).status_code == 401


def test_turn_on_unknown(client: FlaskClient, serve: Serve) -> None:
    """Unknown services return `404`."""
    urls = serve(INFO)
    response = client.put(f"{BASE}/services/UNKNOWN/sync", headers=make_headers(ADMIN))
    assert response.status_code == 404
    assert not urls


@pytest.mark.parametrize("error", [AddressNotAllowed(), Unreachable()])
def test_turn_on_failed_fetch(
    client: FlaskClient, serve: Serve, error: FetchError
) -> None:
    """All failed first fetches look the same, and turn nothing on."""
    identifier = register(client)
    serve(error)
    response = turn_on(client, identifier)
    assert response.status_code == 502
    assert response.json is not None
    assert response.json["detail"] == service_import.FETCH_FAILED
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404


def test_turn_on_invalid(client: FlaskClient, serve: Serve) -> None:
    """Invalid service info turns nothing on."""
    identifier = register(client)
    serve(info(name=None))
    assert turn_on(client, identifier).status_code == 400
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404


def test_turn_on_counts_for_limit(client: FlaskClient, serve: Serve) -> None:
    """Turning it on counts for the limit of fetches."""
    service_info_settings(client).fetches_per_hour = 1
    serve(INFO)
    identifier = register(client)
    assert turn_on(client, identifier).status_code == 200
    assert turn_on(client, identifier).status_code == 429


@pytest.mark.parametrize(
    "fields",
    [{"type": {**TYPE, "artifact": "wes"}}, {"type": {**TYPE, "group": "org.other"}}],
)
def test_turn_on_other_api(
    client: FlaskClient, serve: Serve, fields: dict[str, Any]
) -> None:
    """Service info of another API turns nothing on, and adds no type."""
    identifier = register(client)
    serve(info(**fields))
    response = turn_on(client, identifier)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["code"] == "sync-identity-mismatch"
    types = client.get(f"{BASE}/services/types").json
    assert fields["type"] not in (types or [])


def test_turn_on_new_identifier_needs_off(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """A new identifier needs sync to be turned off first."""
    serve(info(id="org.example.other"))
    response = turn_on(client, service_id)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["code"] == "sync-identity-mismatch"
    assert state(client, service_id)["serviceInfoId"] == INFO["id"]
    assert (
        client.delete(
            f"{BASE}/services/{service_id}/sync", headers=make_headers()
        ).status_code
        == 200
    )
    assert turn_on(client, service_id).status_code == 200
    assert state(client, service_id)["serviceInfoId"] == "org.example.other"


def test_turn_on_again_syncs_now(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Repeating the request syncs at once."""
    serve(info(description="Newer."))
    assert turn_on(client, service_id).status_code == 200
    assert get_service(client, service_id)["description"] == "Newer."


def test_only_if_enabled_syncs(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """With `onlyIfEnabled`, a service with sync syncs at once."""
    serve(info(description="Newer."))
    response = client.put(
        f"{BASE}/services/{service_id}/sync?onlyIfEnabled=true",
        headers=make_headers(USER, roles=None),
    )
    assert response.status_code == 200
    assert get_service(client, service_id)["description"] == "Newer."


def test_only_if_enabled_stays_off(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """With `onlyIfEnabled`, a service without sync stays so, without a fetch."""
    headers = make_headers(USER, roles=None)
    off = client.delete(f"{BASE}/services/{service_id}/sync", headers=headers)
    assert off.status_code == 200
    urls = serve(info(description="Newer."))
    response = client.put(
        f"{BASE}/services/{service_id}/sync?onlyIfEnabled=true", headers=headers
    )
    assert response.status_code == 404
    assert not urls
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 404
    assert get_service(client, service_id)["description"] != "Newer."


def test_only_if_enabled_turned_off_during_fetch(
    client: FlaskClient,
    serve: Serve,
    service_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync turned off during the fetch of `onlyIfEnabled` stays off."""
    serve(info(description="Newer."))
    fetch = sync._fetch

    def turning_off(url: str) -> tuple[str, dict[str, Any]]:
        """Turn sync off during the fetch.

        Args:
            url: URL.

        Returns:
            The result of the fetch.
        """
        services(client).update_one({"id": service_id}, {"$unset": {sync.SYNC: ""}})
        return fetch(url)

    monkeypatch.setattr(sync, "_fetch", turning_off)
    response = client.put(
        f"{BASE}/services/{service_id}/sync?onlyIfEnabled=true",
        headers=make_headers(USER, roles=None),
    )
    assert response.status_code == 404
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 404


def test_only_if_enabled_unavailable(client: FlaskClient, serve: Serve) -> None:
    """With `onlyIfEnabled`, a deployment without sync still says so."""
    identifier = register(client)
    service_info_settings(client).sync = False
    response = client.put(
        f"{BASE}/services/{identifier}/sync?onlyIfEnabled=true",
        headers=make_headers(USER, roles=None),
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "sync-unavailable"


def test_turn_on_own_entry(client: FlaskClient, serve: Serve) -> None:
    """The registry's own entry follows its service info instead."""
    urls = serve(INFO)
    identifier = register(client)
    services(client).update_one({"id": identifier}, {"$set": {SELF_ENTRY: True}})
    response = turn_on(client, identifier)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["code"] == "sync-unavailable"
    assert not urls


def test_turn_on_deleted_meanwhile(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service deleted during the fetch returns `404`."""
    identifier = register(client)
    serve(INFO)
    fetch = sync._fetch

    def deleting(url: str) -> tuple[str, dict[str, Any]]:
        """Delete the service during the fetch.

        Args:
            url: URL.

        Returns:
            The result of the fetch.
        """
        services(client).delete_one({"id": identifier})
        return fetch(url)

    monkeypatch.setattr(sync, "_fetch", deleting)
    assert turn_on(client, identifier).status_code == 404


def test_turn_on_gone_after_claim(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service deleted while it is applied returns `404`."""
    identifier = register(client)
    serve(INFO)

    def deleting(*args: Any) -> None:
        """Delete the service instead of applying.

        Args:
            *args: Ignored.
        """
        services(client).delete_one({"id": identifier})

    monkeypatch.setattr(sync, "_apply", deleting)
    assert turn_on(client, identifier).status_code == 404


def test_turn_off(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """Turned off, the service keeps its values and is no longer synced."""
    urls = serve(info(description="Newer."))
    url = f"{BASE}/services/{service_id}/sync"
    response = client.delete(url, headers=make_headers())
    assert response.status_code == 200
    assert response.json == service_id
    assert client.get(url).status_code == 404
    assert client.delete(url, headers=make_headers()).status_code == 404
    assert run_syncs(client) == 0
    assert not urls
    assert get_service(client, service_id)["description"] == INFO["description"]


def test_label_absent(client: FlaskClient, serve: Serve) -> None:
    """Services without sync have no label."""
    identifier = register(client)
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404
    assert client.get(f"{BASE}/syncs").json == []


def test_public_read(client: FlaskClient, service_id: str) -> None:
    """Anyone may read the state; invalid tokens are rejected."""
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 200
    assert client.get(f"{BASE}/syncs").status_code == 200
    headers = {"Authorization": "Bearer invalid"}
    for path in (f"services/{service_id}/sync", "syncs"):
        assert client.get(f"{BASE}/{path}", headers=headers).status_code == 401


def test_syncs_filter(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """Clients can filter by the label; the list has the identifiers."""
    other = {**SERVICE, "url": "https://other.example.org/ga4gh/tes/v1"}
    other_id = register(client, other)
    assert turn_on(client, other_id).status_code == 200
    serve(Unreachable())
    services(client).update_one(
        {"id": other_id}, {"$set": {f"{SYNC}.retries": sync.RETRIES}}
    )
    services(client).update_one(
        {"id": service_id},
        {"$set": {sync.NEXT: now() + timedelta(hours=1)}},
    )
    services(client).update_one(
        {"id": other_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        assert sync.run_due_syncs() == 1
    listed = client.get(f"{BASE}/syncs").json
    assert listed is not None
    assert [item["serviceId"] for item in listed] == sorted([service_id, other_id])
    synced = client.get(f"{BASE}/syncs?status=synced").json
    assert [item["serviceId"] for item in synced or []] == [service_id]
    out = client.get(f"{BASE}/syncs?status=out-of-sync").json
    assert [item["serviceId"] for item in out or []] == [other_id]
    assert out is not None
    assert out[0]["reason"] == "unreachable"
    assert client.get(f"{BASE}/syncs?status=other").status_code == 400


def test_sync_applies(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """Syncs set descriptive fields and remove optional ones that are gone."""
    served = {key: value for key, value in INFO.items() if key != "description"}
    urls = serve(
        {
            **served,
            "name": "New name",
            "organization": {"name": "New", "url": "https://new.example.org"},
            "contactUrl": "mailto:new@example.org",
            "documentationUrl": "https://docs.example.org",
            "environment": "test",
            "version": "2.0.0",
        }
    )
    before = get_service(client, service_id)
    time.sleep(0.002)
    assert run_syncs(client) == 1
    after = get_service(client, service_id)
    assert after["name"] == "New name"
    assert after["organization"]["name"] == "New"
    assert after["contactUrl"] == "mailto:new@example.org"
    assert after["documentationUrl"] == "https://docs.example.org"
    assert after["environment"] == "test"
    assert after["version"] == "2.0.0"
    assert "description" not in after
    assert after["updatedAt"] > before["updatedAt"]
    assert after["createdAt"] == before["createdAt"]
    assert urls == [f"{URL}/service-info"]


def test_sync_without_change(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """`updatedAt` stays if no value changes; the attempt is recorded."""
    serve(INFO)
    before = get_service(client, service_id)
    stored = state(client, service_id)
    time.sleep(0.002)
    assert run_syncs(client) == 1
    assert get_service(client, service_id) == before
    after = state(client, service_id)
    assert after["lastSuccessAt"] > stored["lastSuccessAt"]


def test_sync_never_changes_identity(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Syncs change neither the identifier, the URL nor the owners."""
    serve(info(url="https://elsewhere.example.org", owner="x"))
    permissions = client.get(
        f"{BASE}/services/{service_id}/permissions", headers=make_headers()
    ).json
    assert run_syncs(client) == 1
    service = get_service(client, service_id)
    assert service["id"] == service_id
    assert service["url"] == URL
    assert (
        client.get(
            f"{BASE}/services/{service_id}/permissions", headers=make_headers()
        ).json
        == permissions
    )


def test_verified_stays(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """The verification stays through syncs, also with a new version."""
    assert (
        client.put(
            f"{BASE}/services/{service_id}/verification",
            headers=make_headers(ADMIN),
        ).status_code
        == 200
    )
    serve(info(type=new_type(), name="New"))
    assert run_syncs(client) == 1
    assert get_service(client, service_id)["type"] == new_type()
    assert client.get(f"{BASE}/services/{service_id}/verification").status_code == 200
    stored = services(client).find_one({"id": service_id})
    assert stored is not None
    assert VERIFIED in stored


@pytest.mark.parametrize(
    ("error", "reason"),
    [(AddressNotAllowed(), "address not allowed")],
)
def test_failure_keeps_values(
    client: FlaskClient,
    serve: Serve,
    service_id: str,
    error: FetchError,
    reason: str,
) -> None:
    """After a failure, the last values stay; the reason is served."""
    before = get_service(client, service_id)
    success = state(client, service_id)["lastSuccessAt"]
    serve(error)
    assert run_syncs(client) == 1
    assert get_service(client, service_id) == before
    current = state(client, service_id)
    assert current["status"] == "out-of-sync"
    assert current["reason"] == reason
    assert current["lastSuccessAt"] == success


def test_invalid_service_info(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Invalid service info keeps the values, also for a disallowed environment."""
    before = get_service(client, service_id)
    serve(info(environment="other", name="New"))
    assert run_syncs(client) == 1
    assert get_service(client, service_id) == before
    assert state(client, service_id)["reason"] == "invalid service info"


def test_recovers(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """A success ends the failure."""
    serve(AddressNotAllowed())
    run_syncs(client)
    assert "failingSince" in stored_state(client, service_id)
    serve(INFO)
    run_syncs(client)
    current = state(client, service_id)
    assert current["status"] == "synced"
    assert "reason" not in current
    assert "failingSince" not in stored_state(client, service_id)


def test_failing_since_kept(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """The time a problem started stays while it lasts, whatever the reasons."""
    serve(AddressNotAllowed())
    run_syncs(client)
    since = stored_state(client, service_id)["failingSince"]
    serve(info(id="org.example.other"))
    time.sleep(0.002)
    run_syncs(client)
    assert state(client, service_id)["reason"] == "identity mismatch"
    assert stored_state(client, service_id)["failingSince"] == since


def test_retries_before_failure_counts(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """An unreachable service stays synced until the retries of a cycle are over."""
    serve(Unreachable())
    for retries in range(1, sync.RETRIES + 1):
        run_syncs(client)
        stored = stored_state(client, service_id)
        assert stored["retries"] == retries
        wait = stored[sync.NEXT_ATTEMPT].replace(tzinfo=UTC) - now()
        assert (
            timedelta(minutes=5 * retries - 1) < wait <= timedelta(minutes=5 * retries)
        )
        assert state(client, service_id)["status"] == "synced"
    run_syncs(client)
    current = state(client, service_id)
    assert current["status"] == "out-of-sync"
    assert current["reason"] == "unreachable"
    stored = stored_state(client, service_id)
    assert stored["retries"] == 0
    assert stored[sync.NEXT_ATTEMPT].replace(tzinfo=UTC) - now() > timedelta(hours=23)


def test_retry_at_most_an_interval(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Retries wait at most an interval."""
    service_info_settings(client).sync_interval_hours = 0.01
    serve(Unreachable())
    run_syncs(client)
    wait = (
        stored_state(client, service_id)[sync.NEXT_ATTEMPT].replace(tzinfo=UTC) - now()
    )
    assert wait <= timedelta(hours=0.01)


def test_retries_end_with_success(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """A success in the retries starts the next cycle without them."""
    serve(Unreachable())
    run_syncs(client)
    serve(INFO)
    run_syncs(client)
    assert stored_state(client, service_id)["retries"] == 0


def test_stale_success_is_out_of_sync(client: FlaskClient, service_id: str) -> None:
    """A scheduler that stopped can't show `synced` forever: no reason then."""
    interval = timedelta(hours=24)
    services(client).update_one(
        {"id": service_id},
        {
            "$set": {
                f"{SYNC}.lastSuccessAt": now() - 2 * interval + timedelta(minutes=1)
            }
        },
    )
    assert state(client, service_id)["status"] == "synced"
    services(client).update_one(
        {"id": service_id},
        {
            "$set": {
                f"{SYNC}.lastSuccessAt": now() - 2 * interval - timedelta(minutes=1)
            }
        },
    )
    current = state(client, service_id)
    assert current["status"] == "out-of-sync"
    assert "reason" not in current


def test_never_succeeded_is_out_of_sync(client: FlaskClient, service_id: str) -> None:
    """Without a success (e.g., a pending type at first), it is out of sync."""
    services(client).update_one(
        {"id": service_id}, {"$unset": {f"{SYNC}.lastSuccessAt": ""}}
    )
    current = state(client, service_id)
    assert current["status"] == "out-of-sync"
    assert "lastSuccessAt" not in current


def test_identity_mismatch_identifier(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Service info with another identifier is not applied, in any mode."""
    before = get_service(client, service_id)
    serve(info(id="org.example.other", name="New"))
    run_syncs(client)
    assert get_service(client, service_id) == before
    assert state(client, service_id)["reason"] == "identity mismatch"


@pytest.mark.parametrize("mode", ["open", "moderated", "controlled"])
@pytest.mark.parametrize("field", ["group", "artifact"])
def test_identity_mismatch_type(
    client: FlaskClient, serve: Serve, service_id: str, mode: str, field: str
) -> None:
    """Another group or artifact never creates a type, in any mode."""
    set_access_control(client, service_types=mode)
    other = {**TYPE, field: "other"}
    serve(info(type=other))
    run_syncs(client)
    assert state(client, service_id)["reason"] == "identity mismatch"
    assert other not in (
        client.get(f"{BASE}/services/types", headers=make_headers(ADMIN)).json or []
    )


def test_case_of_type_is_no_mismatch(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Group and artifact compare as for duplicates: case-insensitively."""
    other = {**TYPE, "artifact": "TES"}
    serve(info(type=other))
    run_syncs(client)
    assert state(client, service_id)["status"] == "synced"
    assert get_service(client, service_id)["type"] == other


def test_new_version_open(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """In mode `open`, a new version is added and applied."""
    serve(info(type=new_type()))
    run_syncs(client)
    assert state(client, service_id)["status"] == "synced"
    assert get_service(client, service_id)["type"] == new_type()
    assert new_type() in (client.get(f"{BASE}/services/types").json or [])


def test_new_version_moderated(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """In mode `moderated`, it waits for approval, then the next sync applies it."""
    set_access_control(client, service_types="moderated")
    before = get_service(client, service_id)
    serve(info(type=new_type(), name="New"))
    run_syncs(client)
    assert get_service(client, service_id) == before
    current = state(client, service_id)
    assert current["status"] == "out-of-sync"
    assert current["reason"] == "version awaiting approval"
    # still listed to others, with the old values
    listed = client.get(f"{BASE}/services").json or []
    assert service_id in [item["id"] for item in listed]
    approvals = client.get(f"{BASE}/approvals", headers=make_headers(ADMIN)).json
    assert approvals is not None
    assert new_type() in approvals["serviceTypes"]
    assert approvals["services"] == []
    # visible to the user the service counts for
    own = client.get(f"{BASE}/approvals", headers=make_headers(roles=None)).json
    assert own is not None
    assert new_type() in own["serviceTypes"]
    # not applied before the approval
    run_syncs(client)
    assert state(client, service_id)["reason"] == "version awaiting approval"
    assert (
        client.put(
            f"{BASE}/services/types/approval",
            json=new_type(),
            headers=make_headers(ADMIN),
        ).status_code
        == 200
    )
    run_syncs(client)
    assert state(client, service_id)["status"] == "synced"
    service = get_service(client, service_id)
    assert service["type"] == new_type()
    assert service["name"] == "New"


def test_new_version_moderated_limit(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Pending types count for the limit of the user the service counts for."""
    set_access_control(client, service_types="moderated", max_unused_service_types=1)
    serve(info(type=new_type()))
    run_syncs(client)
    assert state(client, service_id)["reason"] == "version awaiting approval"
    serve(info(type=new_type("1.3.0")))
    run_syncs(client)
    assert state(client, service_id)["reason"] == "unused types limit reached"
    types = client.get(f"{BASE}/services/types", headers=make_headers(ADMIN)).json
    assert new_type("1.3.0") not in (types or [])


def test_new_version_open_limit(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """The limit also holds in mode `open`, as on registrations."""
    set_access_control(client, max_unused_service_types=1)
    assert (
        client.post(
            f"{BASE}/services/types",
            json=new_type("0.1.0"),
            headers=make_headers(),
        ).status_code
        == 201
    )
    serve(info(type=new_type()))
    run_syncs(client)
    assert state(client, service_id)["reason"] == "unused types limit reached"


@pytest.mark.parametrize(
    "settings",
    [{"service_types": "controlled"}, {"max_unused_service_types": 0}],
    ids=["controlled", "limit 0"],
)
def test_new_version_not_allowed(
    client: FlaskClient, serve: Serve, service_id: str, settings: dict[str, Any]
) -> None:
    """If only admins may add types, nothing is created."""
    set_access_control(client, **settings)
    before = get_service(client, service_id)
    serve(info(type=new_type()))
    run_syncs(client)
    assert get_service(client, service_id) == before
    assert state(client, service_id)["reason"] == "version not allowed"
    types = client.get(f"{BASE}/services/types", headers=make_headers(ADMIN)).json
    assert new_type() not in (types or [])


def test_stored_version_controlled(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """A registered version is applied also if only admins may add types."""
    set_access_control(client, service_types="controlled")
    assert (
        client.post(
            f"{BASE}/services/types", json=new_type(), headers=make_headers(ADMIN)
        ).status_code
        == 201
    )
    serve(info(type=new_type()))
    run_syncs(client)
    assert state(client, service_id)["status"] == "synced"
    assert get_service(client, service_id)["type"] == new_type()


def test_admin_entry_moderated(client: FlaskClient, serve: Serve) -> None:
    """Admins' entries are moderated too, and each has its own limit."""
    set_access_control(client, service_types="moderated", max_unused_service_types=1)
    serve(INFO)
    identifier = register(client, user=ADMIN)
    assert turn_on(client, identifier, user=ADMIN).status_code == 200
    serve(info(type=new_type("1.2.0")))
    run_syncs(client)
    assert state(client, identifier)["reason"] == "version awaiting approval"
    serve(info(type=new_type("1.3.0")))
    run_syncs(client)
    assert state(client, identifier)["reason"] == "unused types limit reached"
    types = client.get(f"{BASE}/services/types", headers=make_headers(ADMIN)).json
    assert new_type("1.3.0") not in (types or [])


def test_turn_on_url_changed_during_fetch(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URL changed during the first fetch never gets fetched by sync."""
    serve(INFO)
    identifier = register(client)
    fetch = sync._fetch

    def changing_url(url: str) -> tuple[str, dict[str, Any]]:
        """Change the URL during the fetch.

        Args:
            url: URL.

        Returns:
            The result of the fetch.
        """
        result = fetch(url)
        response = client.put(
            f"{BASE}/services/{identifier}",
            json={**SERVICE, "url": "https://internal.example.org/x"},
            headers=make_headers(),
        )
        assert response.status_code == 200
        return result

    monkeypatch.setattr(sync, "_fetch", changing_url)
    response = turn_on(client, identifier)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["code"] == "service-busy"
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404


def test_turn_on_other_change_during_fetch(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other changes during the first fetch aren't overwritten silently."""
    serve(INFO)
    identifier = register(client)
    fetch = sync._fetch

    def changing_name(url: str) -> tuple[str, dict[str, Any]]:
        """Change the name during the fetch.

        Args:
            url: URL.

        Returns:
            The result of the fetch.
        """
        result = fetch(url)
        time.sleep(0.002)
        response = client.put(
            f"{BASE}/services/{identifier}",
            json={**SERVICE, "name": "By hand"},
            headers=make_headers(),
        )
        assert response.status_code == 200
        return result

    monkeypatch.setattr(sync, "_fetch", changing_name)
    assert turn_on(client, identifier).status_code == 409
    assert get_service(client, identifier)["name"] == "By hand"


@pytest.mark.parametrize("user", [USER, ADMIN], ids=["user", "admin"])
def test_new_versions_keep_syncing(
    client: FlaskClient, serve: Serve, user: str
) -> None:
    """Versions that a service leaves behind don't use up the limit."""
    set_access_control(client, max_unused_service_types=1)
    serve(INFO)
    identifier = register(client, user=user)
    assert turn_on(client, identifier, user=user).status_code == 200
    versions = ("1.2.0", "1.3.0", "1.4.0", "1.5.0")
    for version in versions:
        serve(info(type=new_type(version)))
        run_syncs(client)
        assert state(client, identifier)["status"] == "synced"
        assert get_service(client, identifier)["type"] == new_type(version)
    types = client.get(f"{BASE}/services/types", headers=make_headers(ADMIN)).json
    assert new_type("1.2.0") in (types or [])


def test_left_type_of_another_service_counts(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """A type that another service still has stays with who added it."""
    with client.application.app_context():
        collection = get_collection("service_types")
        collection.insert_one({**new_type(), "createdBy": USER})
        other = {
            **SERVICE,
            "type": new_type(),
            "url": "https://other.example.org/ga4gh/tes/v1",
        }
        register(client, other)
        serve(info(type=new_type()))
        run_syncs(client)
        serve(INFO)
        run_syncs(client)
        stored = collection.find_one(new_type())
    assert stored is not None
    assert stored["createdBy"] == USER


def test_turn_on_turned_off_during_fetch(
    client: FlaskClient, serve: Serve, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning it off during a repeated request stays."""
    fetch = sync._fetch

    def turning_off(url: str) -> tuple[str, dict[str, Any]]:
        """Turn sync off during the fetch.

        Args:
            url: URL.

        Returns:
            The result of the fetch.
        """
        result = fetch(url)
        services(client).update_one({"id": service_id}, {"$unset": {SYNC: ""}})
        return result

    monkeypatch.setattr(sync, "_fetch", turning_off)
    assert turn_on(client, service_id).status_code == 409
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 404


def test_turn_on_turned_off_while_applying(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync turned off right after it was applied returns `404`."""
    identifier = register(client)
    serve(INFO)

    def turning_off(*args: Any) -> None:
        """Turn sync off instead of applying.

        Args:
            *args: Ignored.
        """
        services(client).update_one({"id": identifier}, {"$unset": {SYNC: ""}})

    monkeypatch.setattr(sync, "_apply", turning_off)
    assert turn_on(client, identifier).status_code == 404


def test_identifier_that_cant_be_stored(client: FlaskClient, serve: Serve) -> None:
    """An identifier with unpaired surrogates returns `400`, not `500`."""
    identifier = register(client)
    serve(info(id="\ud800x"))
    assert turn_on(client, identifier).status_code == 400
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404


@pytest.mark.parametrize(
    "change",
    [
        {"type": {**TYPE, "artifact": "wes"}, "name": "By hand"},
        {"name": "By hand"},
    ],
    ids=["other key", "other value"],
)
def test_update_during_sync_wins(
    client: FlaskClient, serve: Serve, service_id: str, change: dict[str, Any]
) -> None:
    """Results aren't written if the service changed since the claim."""
    serve(info(description="From the sync."))
    services(client).update_one(
        {"id": service_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        claimed = sync._claim()
        assert claimed is not None
        time.sleep(0.002)
        assert (
            client.put(
                f"{BASE}/services/{service_id}",
                json={**SERVICE, **change},
                headers=make_headers(),
            ).status_code
            == 200
        )
        sync.sync_service(claimed)
    service = get_service(client, service_id)
    assert service["name"] == "By hand"
    assert service["description"] == SERVICE["description"]
    assert service["type"] == change.get("type", TYPE)


def test_new_url_without_sync_ends_sync(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """A URL changed while the deployment didn't sync is never fetched by sync."""
    settings = service_info_settings(client)
    settings.sync = False
    assert (
        client.put(
            f"{BASE}/services/{service_id}",
            json={**SERVICE, "url": "https://internal.example.org/x"},
            headers=make_headers(),
        ).status_code
        == 200
    )
    settings.sync = True
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 404
    urls = serve(INFO)
    assert run_syncs(client) == 0
    assert not urls


def test_same_url_without_sync_keeps_sync(client: FlaskClient, service_id: str) -> None:
    """Updates that keep the URL keep the state, whatever the deployment does."""
    settings = service_info_settings(client)
    settings.sync = False
    assert (
        client.put(
            f"{BASE}/services/{service_id}",
            json={**SERVICE, "name": "By hand"},
            headers=make_headers(),
        ).status_code
        == 200
    )
    settings.sync = True
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 200


def test_turn_on_pending_version_keeps_on(client: FlaskClient, serve: Serve) -> None:
    """Turning it on with a version awaiting approval has a reason."""
    set_access_control(client, service_types="moderated")
    identifier = register(client, user=ADMIN)
    serve(info(type=new_type()))
    response = turn_on(client, identifier, user=ADMIN)
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["reason"] == "version awaiting approval"
    assert "lastSuccessAt" not in response.json


def test_pending_service_leaves_for_approved_type(
    client: FlaskClient, serve: Serve
) -> None:
    """A pending service becomes visible once it syncs to an approved type."""
    set_access_control(client, service_types="moderated")
    serve(INFO)
    pending = {**SERVICE, "type": new_type("0.9.0")}
    identifier = register(client, pending)
    assert client.get(f"{BASE}/services/{identifier}").status_code == 404
    assert (
        client.post(
            f"{BASE}/services/types", json=TYPE, headers=make_headers(ADMIN)
        ).status_code
        == 201
    )
    assert turn_on(client, identifier).status_code == 200
    assert get_service(client, identifier)["type"] == TYPE


def test_pending_service_hidden(client: FlaskClient, serve: Serve) -> None:
    """The state of a pending service is hidden like the service."""
    set_access_control(client, service_types="moderated")
    pending = {**SERVICE, "type": new_type("0.9.0")}
    identifier = register(client, pending)
    serve(info(type=new_type("0.9.0")))
    response = turn_on(client, identifier)
    assert response.status_code == 200
    assert response.json is not None
    assert response.json["reason"] == "version awaiting approval"
    assert client.get(f"{BASE}/services/{identifier}/sync").status_code == 404
    assert client.get(f"{BASE}/syncs").json == []
    assert (
        client.get(
            f"{BASE}/services/{identifier}/sync", headers=make_headers(roles=None)
        ).status_code
        == 200
    )


def test_url_locked(client: FlaskClient, service_id: str) -> None:
    """With sync on, another URL is rejected, also for admins and other spellings."""
    url = f"{BASE}/services/{service_id}"
    for user in (USER, ADMIN):
        for other in ("https://elsewhere.example.org/tes", f"{URL}/"):
            response = client.put(
                url, json={**SERVICE, "url": other}, headers=make_headers(user)
            )
            assert response.status_code == 409
            assert response.json is not None
            assert response.json["code"] == "sync-url-locked"
    assert get_service(client, service_id)["url"] == URL


def test_url_locked_races(
    client: FlaskClient, service_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync turned on after the check still locks the URL."""
    from cloud_registry.ga4gh.registry.service import RegisterService  # noqa: PLC0415

    calls: list[None] = []
    original = RegisterService._check_url_lock

    def skip_first(self: RegisterService, stored: dict[str, Any]) -> None:
        """Skip the check in `validate()`.

        Args:
            self: Registration.
            stored: Stored service.
        """
        calls.append(None)
        if len(calls) > 1:
            original(self, stored)

    monkeypatch.setattr(RegisterService, "_check_url_lock", skip_first)
    response = client.put(
        f"{BASE}/services/{service_id}",
        json={**SERVICE, "url": "https://elsewhere.example.org/tes"},
        headers=make_headers(),
    )
    assert response.status_code == 409
    assert len(calls) == 2


def test_url_free_after_turning_off(client: FlaskClient, service_id: str) -> None:
    """Turned off, the URL can change."""
    assert (
        client.delete(
            f"{BASE}/services/{service_id}/sync", headers=make_headers()
        ).status_code
        == 200
    )
    other = "https://elsewhere.example.org/tes"
    assert (
        client.put(
            f"{BASE}/services/{service_id}",
            json={**SERVICE, "url": other},
            headers=make_headers(),
        ).status_code
        == 200
    )


def test_update_keeps_sync(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """Updates stay allowed, keep sync on, and last until the next sync."""
    serve(INFO)
    assert (
        client.put(
            f"{BASE}/services/{service_id}",
            json={**SERVICE, "name": "By hand"},
            headers=make_headers(),
        ).status_code
        == 200
    )
    assert get_service(client, service_id)["name"] == "By hand"
    assert state(client, service_id)["status"] == "synced"
    run_syncs(client)
    assert get_service(client, service_id)["name"] == INFO["name"]


def test_rename_keeps_sync(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """Renames keep the state, and syncs go on under the new identifier."""
    assert (
        client.post(
            f"{BASE}/services/{service_id}/rename",
            json={"id": "NEWNAME"},
            headers=make_headers(ADMIN),
        ).status_code
        == 200
    )
    serve(info(description="After."))
    assert run_syncs(client) == 1
    assert get_service(client, "NEWNAME")["description"] == "After."


def test_delete_during_sync(client: FlaskClient, serve: Serve, service_id: str) -> None:
    """A service deleted during its fetch isn't brought back."""
    serve(INFO)
    services(client).update_one(
        {"id": service_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        claimed = sync._claim()
        assert claimed is not None
        services(client).delete_one({"id": service_id})
        sync.sync_service(claimed)
    assert services(client).count_documents({"id": service_id}) == 0


def test_changes_during_sync_win(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Results aren't written if sync was turned off or on again meanwhile."""
    serve(info(name="New"))
    services(client).update_one(
        {"id": service_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        claimed = sync._claim()
        assert claimed is not None
        assert (
            client.delete(
                f"{BASE}/services/{service_id}/sync", headers=make_headers()
            ).status_code
            == 200
        )
        sync.sync_service(claimed)
    assert get_service(client, service_id)["name"] == INFO["name"]


def test_claim_once(client: FlaskClient, service_id: str) -> None:
    """A due service can only be claimed once, until the claim expires."""
    services(client).update_one(
        {"id": service_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        assert sync._claim() is not None
        assert sync._claim() is None
    services(client).update_one(
        {"id": service_id}, {"$set": {sync.NEXT: now() - timedelta(seconds=1)}}
    )
    with client.application.app_context():
        assert sync._claim() is not None


def test_not_due(client: FlaskClient, service_id: str) -> None:
    """Services that aren't due are left alone."""
    with client.application.app_context():
        assert sync.run_due_syncs() == 0


def test_failure_of_a_service_is_contained(
    client: FlaskClient, serve: Serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing sync doesn't keep others from being synced."""
    serve(INFO)
    ids = [
        register(client, {**SERVICE, "url": f"https://s{index}.example.org/tes"})
        for index in range(2)
    ]
    for identifier in ids:
        assert turn_on(client, identifier).status_code == 200
    original = sync.sync_service
    seen: list[str] = []

    def failing(service: dict[str, Any]) -> None:
        """Fail for the first service.

        Args:
            service: Service.

        Raises:
            RuntimeError: For the first service.
        """
        seen.append(service["id"])
        if len(seen) == 1:
            raise RuntimeError("database gone")
        original(service)

    monkeypatch.setattr(sync, "sync_service", failing)
    assert run_syncs(client) == 2
    assert sorted(seen) == sorted(ids)


def test_cap_next_attempts(client: FlaskClient, service_id: str) -> None:
    """A shorter interval applies on startup; nearer attempts stay."""
    service_info_settings(client).sync_interval_hours = 1
    with client.application.app_context():
        sync.cap_next_attempts()
    wait = (
        stored_state(client, service_id)[sync.NEXT_ATTEMPT].replace(tzinfo=UTC) - now()
    )
    assert wait <= timedelta(hours=1)
    near = now() + timedelta(minutes=5)
    services(client).update_one({"id": service_id}, {"$set": {sync.NEXT: near}})
    with client.application.app_context():
        sync.cap_next_attempts()
    assert (
        stored_state(client, service_id)[sync.NEXT_ATTEMPT].replace(tzinfo=UTC) == near
    )


def test_deployment_without_sync(
    client: FlaskClient, serve: Serve, service_id: str
) -> None:
    """Without sync, nothing is fetched, labels are gone and URLs free."""
    settings = service_info_settings(client)
    settings.sync = False
    urls = serve(INFO)
    response = turn_on(client, service_id)
    assert response.status_code == 409
    assert response.json is not None
    assert response.json["code"] == "sync-unavailable"
    assert not urls
    assert client.get(f"{BASE}/services/{service_id}/sync").status_code == 404
    assert client.get(f"{BASE}/syncs").json == []
    assert (
        client.put(
            f"{BASE}/services/{service_id}",
            json={**SERVICE, "url": "https://elsewhere.example.org/tes"},
            headers=make_headers(),
        ).status_code
        == 200
    )
    with client.application.app_context():
        sync.cap_next_attempts()
        assert sync.start_sync_scheduler(client.application) is None
    rules = client.get(f"{BASE}/service-info").json
    assert rules is not None
    assert rules["registrationRules"]["serviceInfoSync"] == {
        "enabled": False,
        "intervalHours": 24.0,
    }


def test_rules(client: FlaskClient) -> None:
    """The service info tells clients whether and how often services are synced."""
    rules = client.get(f"{BASE}/service-info").json
    assert rules is not None
    assert rules["registrationRules"]["serviceInfoSync"] == {
        "enabled": True,
        "intervalHours": 24,
    }


def test_settings() -> None:
    """On by default, every 24 hours; the interval must be within its bounds."""
    settings = ServiceInfoConfig()
    assert settings.sync is True
    assert settings.sync_interval_hours == 24
    for hours in (0, -1, 0.5, 8761, 1e9, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ServiceInfoConfig(sync_interval_hours=hours)
    for hours in (MIN_SYNC_INTERVAL_HOURS, MAX_SYNC_INTERVAL_HOURS, 1, 8760):
        assert ServiceInfoConfig(sync_interval_hours=hours).sync_interval_hours == hours
    for hours in (0.9999, 8760.0001):
        with pytest.raises(ValueError):
            ServiceInfoConfig(sync_interval_hours=hours)


def test_status_with_given_time(app: App) -> None:
    """The label compares with the time given, and copes with naive times."""
    with app.app.app_context():
        current = datetime(2026, 11, 5, tzinfo=UTC)
        aware = {"lastSuccessAt": current}
        naive = {"lastSuccessAt": current.replace(tzinfo=None)}
        for item in (aware, naive):
            assert sync.sync_status(item, current) is sync.Status.SYNCED
        later = current + timedelta(hours=49)
        assert sync.sync_status(aware, later) is sync.Status.OUT_OF_SYNC


class _Handler(BaseHTTPRequestHandler):
    """Handler of a service that serves service info."""

    body = b"{}"
    requests: list[dict[str, str]] = []  # noqa: RUF012 (for the test)

    def do_GET(self) -> None:
        """Serve the service info, recording the request."""
        _Handler.requests.append(dict(self.headers.items()))
        self.send_response(200)
        self.send_header("Content-Length", str(len(_Handler.body)))
        self.end_headers()
        self.wfile.write(_Handler.body)

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


def test_scheduler_end_to_end(client: FlaskClient, port: int) -> None:
    """The scheduler syncs a local service without any request."""
    with client.application.app_context():
        endpoint = get_custom_config(CustomConfig).endpoints.services
        endpoint.url_schemes = ["http", "https"]
        endpoint.service_info.private_networks = ["127.0.0.0/8"]
        endpoint.service_info.sync_interval_hours = 0.0001
    url = f"http://127.0.0.1:{port}/api"
    _Handler.body = json.dumps(INFO).encode()
    identifier = register(client, {**SERVICE, "url": url})
    assert turn_on(client, identifier).status_code == 200
    assert _Handler.requests
    assert "Authorization" not in _Handler.requests[0]
    _Handler.body = json.dumps(info(description="From the scheduler.")).encode()
    stop = threading.Event()
    thread = threading.Thread(
        target=sync._run_scheduler, args=(client.application, stop), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            stored = services(client).find_one({"id": identifier})
            assert stored is not None
            if stored["description"] == "From the scheduler.":
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(5)
    assert (services(client).find_one({"id": identifier}) or {}).get(
        "description"
    ) == "From the scheduler."
    assert not thread.is_alive()


def test_scheduler_survives_errors(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Errors (e.g., of the database) don't end the scheduler."""
    monkeypatch.setattr(sync, "TICK", 0.01)
    with client.application.app_context():
        endpoint = get_custom_config(CustomConfig).endpoints.services
        endpoint.service_info.sync_interval_hours = 0.0001
    stop = threading.Event()
    calls: list[None] = []

    def failing() -> int:
        """Fail until a few calls were made.

        Returns:
            Never.

        Raises:
            RuntimeError: Always.
        """
        calls.append(None)
        if len(calls) > 2:
            stop.set()
        raise RuntimeError("database gone")

    monkeypatch.setattr(sync, "run_due_syncs", failing)
    sync._run_scheduler(client.application, stop)
    assert len(calls) == 3


def test_start_scheduler(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler is a daemon thread."""
    ran: list[Any] = []
    monkeypatch.setattr(sync, "_run_scheduler", lambda *args: ran.append(args))
    with client.application.app_context():
        thread = sync.start_sync_scheduler(client.application)
    assert thread is not None
    thread.join(5)
    assert thread.daemon
    assert ran
    assert ran[0][0] is client.application


def test_create_app_starts_scheduler(
    app_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app starts the scheduler, with each worker."""
    started: list[Any] = []
    monkeypatch.setattr("cloud_registry.app.start_sync_scheduler", started.append)
    from cloud_registry.app import create_app  # noqa: PLC0415

    app = create_app()
    assert started == [app.app]
