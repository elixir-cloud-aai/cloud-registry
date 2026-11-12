"""Tests for notifications of what needs the attention of users."""

from datetime import timedelta
from typing import Any

from flask.testing import FlaskClient
from foca import get_custom_config
import pytest

from cloud_registry.ga4gh.registry import notifications as notifications_module
from cloud_registry.ga4gh.registry import sync
from cloud_registry.ga4gh.registry.service import PENDING_SERVICE, SYNC
from cloud_registry.ga4gh.registry.service_types import PENDING
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection, now
from tests.conftest import ADMIN, USER, make_headers

BASE = "/ga4gh/registry/v1"
URL = f"{BASE}/notifications"
TYPE = {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}
SERVICE: dict[str, Any] = {
    "name": "My TES",
    "type": TYPE,
    "organization": {"name": "Example", "url": "https://example.org"},
    "version": "1.0.0",
    "url": "https://tes.example.org/ga4gh/tes/v1",
}
OTHER = "bob@example.org"
MAINTAINER = "erin@example.org"


def register(client: FlaskClient, host: str = "tes", user: str = USER) -> str:
    """Register a service.

    Args:
        client: Test client of the app.
        host: First label of the host name of its URL.
        user: User who registers it.

    Returns:
        Identifier of the service.
    """
    response = client.post(
        f"{BASE}/services",
        json={**SERVICE, "url": f"https://{host}.example.org/ga4gh/tes/v1"},
        headers=make_headers(user),
    )
    assert response.status_code == 201
    assert isinstance(response.json, str)
    return response.json


def update_service(
    client: FlaskClient, service_id: str, update: dict[str, Any]
) -> None:
    """Change a stored service directly.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        update: Update.
    """
    with client.application.app_context():
        result = get_collection("services").update_one({"id": service_id}, update)
    assert result.matched_count == 1


def fail(
    client: FlaskClient, service_id: str, reason: str = "unreachable", days: int = 1
) -> None:
    """Make a service out of sync, with a reason.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
        reason: Reason.
        days: Days since the problem started.
    """
    current = now()
    update_service(
        client,
        service_id,
        {
            "$set": {
                SYNC: {
                    "serviceInfoId": "x",
                    "nextAttemptAt": current + timedelta(days=1),
                    "lastAttemptAt": current,
                    "lastSuccessAt": current - timedelta(days=days + 1),
                    "failingSince": current - timedelta(days=days),
                    "reason": reason,
                }
            }
        },
    )


def recover(client: FlaskClient, service_id: str) -> None:
    """Make a service in sync.

    Args:
        client: Test client of the app.
        service_id: Service identifier.
    """
    current = now()
    update_service(
        client,
        service_id,
        {
            "$set": {
                SYNC: {
                    "serviceInfoId": "x",
                    "nextAttemptAt": current + timedelta(days=1),
                    "lastAttemptAt": current,
                    "lastSuccessAt": current,
                }
            }
        },
    )


def notifications(
    client: FlaskClient, user: str = USER, admin: bool = False
) -> list[dict[str, Any]]:
    """List the notifications of a user.

    Args:
        client: Test client of the app.
        user: User.
        admin: Whether the request asks for admin mode.

    Returns:
        Notifications.
    """
    response = client.get(
        URL, headers=make_headers(user, roles="admin" if admin else None)
    )
    assert response.status_code == 200
    assert isinstance(response.json, list)
    return response.json


def dismiss(
    client: FlaskClient, notification_id: str, user: str = USER, admin: bool = False
) -> int:
    """Dismiss a notification.

    Args:
        client: Test client of the app.
        notification_id: Identifier of the notification.
        user: User.
        admin: Whether the request asks for admin mode.

    Returns:
        Status code of the response.
    """
    return client.delete(
        f"{URL}/{notification_id}",
        headers=make_headers(user, roles="admin" if admin else None),
    ).status_code


def add_maintainer(client: FlaskClient, service_id: str) -> None:
    """Make a user a maintainer of a service.

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


def services_config(client: FlaskClient) -> Any:
    """Get the configuration of services, which tests can change.

    Args:
        client: Test client of the app.

    Returns:
        Configuration.
    """
    with client.application.app_context():
        return get_custom_config(CustomConfig).endpoints.services


def dismissals(client: FlaskClient) -> list[dict[str, Any]]:
    """Get the stored dismissals.

    Args:
        client: Test client of the app.

    Returns:
        Dismissals.
    """
    with client.application.app_context():
        return list(get_collection(notifications_module.DISMISSALS).find())


def age_dismissals(client: FlaskClient) -> None:
    """Make the stored dismissals older than any listing that follows.

    Listings keep dismissals from the millisecond they start on (cf.
    `getNotifications()`), which a fast test may share with a dismissal.

    Args:
        client: Test client of the app.
    """
    with client.application.app_context():
        get_collection(notifications_module.DISMISSALS).update_many(
            {}, {"$set": {"dismissedAt": now() - timedelta(minutes=1)}}
        )


def test_requires_login(client: FlaskClient) -> None:
    """Notifications need a login."""
    assert client.get(URL).status_code == 401
    assert client.delete(f"{URL}/x").status_code == 401


def test_nothing_to_report(client: FlaskClient) -> None:
    """Users start without notifications, also with services that are in sync."""
    service_id = register(client)
    assert notifications(client) == []
    recover(client, service_id)
    assert notifications(client) == []
    assert notifications(client, ADMIN, admin=True) == []


def test_out_of_sync_for_owners(client: FlaskClient) -> None:
    """Who has a role on a service sees its problems; others don't."""
    service_id = register(client)
    add_maintainer(client, service_id)
    fail(client, service_id)
    items = notifications(client)
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "out-of-sync"
    assert item["serviceId"] == service_id
    assert item["reason"] == "unreachable"
    assert item["since"].endswith("Z")
    assert "serviceType" not in item
    assert notifications(client, MAINTAINER) == items
    assert notifications(client, OTHER) == []
    # admins act as other users without admin mode
    assert notifications(client, ADMIN) == []
    assert notifications(client, ADMIN, admin=True) == items


def test_no_recent_sync(client: FlaskClient) -> None:
    """A success too long ago is reported without a reason of a sync."""
    service_id = register(client)
    recover(client, service_id)
    update_service(
        client,
        service_id,
        {"$set": {f"{SYNC}.lastSuccessAt": now() - timedelta(days=3)}},
    )
    items = notifications(client)
    assert [item["reason"] for item in items] == ["no recent sync"]
    assert "since" in items[0]


def test_nothing_known_about_the_time(client: FlaskClient) -> None:
    """Notifications without any time have no `since`."""
    service_id = register(client)
    update_service(client, service_id, {"$set": {SYNC: {"serviceInfoId": "x"}}})
    items = notifications(client)
    assert [item["reason"] for item in items] == ["no recent sync"]
    assert "since" not in items[0]


def test_sync_turned_off_in_deployment(client: FlaskClient) -> None:
    """Without sync in the deployment, there are no notifications about it."""
    service_id = register(client)
    fail(client, service_id)
    services_config(client).service_info.sync = False
    assert notifications(client) == []
    assert notifications(client, ADMIN, admin=True) == []


def test_dismissal_shared_by_owners(client: FlaskClient) -> None:
    """Any owner dismisses for all owners, not for admins."""
    service_id = register(client)
    add_maintainer(client, service_id)
    fail(client, service_id)
    notification_id = notifications(client)[0]["id"]
    assert dismiss(client, notification_id, MAINTAINER) == 200
    assert notifications(client) == []
    assert notifications(client, MAINTAINER) == []
    assert len(notifications(client, ADMIN, admin=True)) == 1


def test_dismissal_shared_by_admins(client: FlaskClient) -> None:
    """Any admin dismisses for all admins, not for owners."""
    service_id = register(client)
    fail(client, service_id)
    notification_id = notifications(client)[0]["id"]
    assert dismiss(client, notification_id, ADMIN, admin=True) == 200
    assert notifications(client, ADMIN, admin=True) == []
    assert len(notifications(client)) == 1


def test_dismiss_returns_id(client: FlaskClient) -> None:
    """Dismissing returns the identifier."""
    service_id = register(client)
    fail(client, service_id)
    notification_id = notifications(client)[0]["id"]
    response = client.delete(
        f"{URL}/{notification_id}", headers=make_headers(roles=None)
    )
    assert response.json == notification_id


def test_dismiss_only_what_is_visible(client: FlaskClient) -> None:
    """Others can't dismiss, and nothing can be dismissed twice or made up."""
    service_id = register(client)
    fail(client, service_id)
    notification_id = notifications(client)[0]["id"]
    assert dismiss(client, notification_id, OTHER) == 404
    assert dismiss(client, "unknown") == 404
    assert len(notifications(client)) == 1
    assert dismiss(client, notification_id) == 200
    assert dismiss(client, notification_id) == 404


def test_concurrent_dismissals(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Of dismissals at the same time, one succeeds; the other finds it dismissed."""
    service_id = register(client)
    fail(client, service_id)
    notification_id = notifications(client)[0]["id"]
    assert dismiss(client, notification_id) == 200
    original = notifications_module.get_collection

    class Unseen:
        """Collection whose reads don't see what a concurrent request wrote."""

        def __init__(self, name: str) -> None:
            """Create the collection.

            Args:
                name: Name of the collection.
            """
            self.collection = original(name)

        def __getattr__(self, name: str) -> Any:
            """Get an attribute of the collection.

            Args:
                name: Name of the attribute.

            Returns:
                The attribute.
            """
            return getattr(self.collection, name)

        def find_one(self, *args: Any, **kwargs: Any) -> None:
            """Find nothing.

            Args:
                *args: Ignored.
                **kwargs: Ignored.
            """

    monkeypatch.setattr(notifications_module, "get_collection", Unseen)
    assert dismiss(client, notification_id) == 404


def test_dismissed_stays_while_problem_lasts(client: FlaskClient) -> None:
    """A dismissed problem stays away through further failed attempts."""
    service_id = register(client)
    fail(client, service_id)
    assert dismiss(client, notifications(client)[0]["id"]) == 200
    update_service(client, service_id, {"$set": {f"{SYNC}.lastAttemptAt": now()}})
    assert notifications(client) == []


def test_comes_back_with_another_reason(client: FlaskClient) -> None:
    """A dismissed problem comes back if its reason changes."""
    service_id = register(client)
    fail(client, service_id)
    assert dismiss(client, notifications(client)[0]["id"]) == 200
    update_service(
        client, service_id, {"$set": {f"{SYNC}.reason": "identity mismatch"}}
    )
    items = notifications(client)
    assert [item["reason"] for item in items] == ["identity mismatch"]


def test_comes_back_after_ending(client: FlaskClient) -> None:
    """A dismissed problem comes back if it ended and came back."""
    service_id = register(client)
    fail(client, service_id, days=2)
    notification_id = notifications(client)[0]["id"]
    assert dismiss(client, notification_id) == 200
    recover(client, service_id)
    assert notifications(client) == []
    fail(client, service_id, days=1)
    items = notifications(client)
    assert len(items) == 1
    assert items[0]["id"] != notification_id


def test_rename_keeps_notification(client: FlaskClient) -> None:
    """Renames keep notifications and dismissals, with the new identifier."""
    service_id = register(client)
    fail(client, service_id)
    before = notifications(client)[0]
    response = client.post(
        f"{BASE}/services/{service_id}/rename",
        json={"id": "NEWNAME"},
        headers=make_headers(ADMIN),
    )
    assert response.status_code == 200
    after = notifications(client)[0]
    assert after["id"] == before["id"]
    assert after["serviceId"] == "NEWNAME"


def test_comes_back_with_reason_that_returned(client: FlaskClient) -> None:
    """A dismissed reason comes back after another one, as a real sync records it."""
    service_id = register(client)
    fail(client, service_id)

    def set_reason(reason: sync.Reason) -> None:
        """Record a failed sync.

        Args:
            reason: Reason.
        """
        with client.application.app_context():
            service = get_collection("services").find_one({"id": service_id})
            assert service is not None
            sync._failed(service, reason, now())

    set_reason(sync.Reason.UNREACHABLE)
    first = notifications(client)[0]["id"]
    assert dismiss(client, first) == 200
    set_reason(sync.Reason.UNREACHABLE)
    assert notifications(client) == []
    set_reason(sync.Reason.INVALID)
    second = notifications(client)[0]["id"]
    set_reason(sync.Reason.UNREACHABLE)
    third = notifications(client)[0]["id"]
    assert len({first, second, third}) == 3


def test_admin_types_stay_dismissed(client: FlaskClient) -> None:
    """Updates and renames don't bring back what admins dismissed."""
    service_id = register(client)
    update_service(
        client,
        service_id,
        {
            "$set": {
                PENDING_SERVICE: True,
                "environment": "gone",
                "url": "http://tes.example.org/x",
            }
        },
    )
    items = notifications(client, ADMIN, admin=True)
    assert len(items) == 3
    for item in items:
        assert dismiss(client, item["id"], ADMIN, admin=True) == 200
    update_service(client, service_id, {"$set": {"updatedAt": "2030-01-01T00:00:00Z"}})
    update_service(client, service_id, {"$set": {"id": "NEWNAME"}})
    assert notifications(client, ADMIN, admin=True) == []


def test_admin_types_come_back_with_new_values(client: FlaskClient) -> None:
    """Another value that isn't allowed, or another pending type, comes back."""
    service_id = register(client)
    update_service(
        client,
        service_id,
        {"$set": {PENDING_SERVICE: True, "environment": "gone"}},
    )
    for item in notifications(client, ADMIN, admin=True):
        assert dismiss(client, item["id"], ADMIN, admin=True) == 200
    update_service(
        client,
        service_id,
        {"$set": {"environment": "other", "type": {**TYPE, "version": "9"}}},
    )
    types = {item["type"] for item in notifications(client, ADMIN, admin=True)}
    assert types == {"pending-service", "environment-not-allowed"}


def test_admin_types_come_back_after_ending(client: FlaskClient) -> None:
    """What ended while admins looked comes back, with the same type or value."""
    service_id = register(client)
    problem = {PENDING_SERVICE: True, "environment": "gone"}
    update_service(client, service_id, {"$set": problem})
    for item in notifications(client, ADMIN, admin=True):
        assert dismiss(client, item["id"], ADMIN, admin=True) == 200
    assert notifications(client, ADMIN, admin=True) == []
    update_service(
        client, service_id, {"$unset": {PENDING_SERVICE: "", "environment": ""}}
    )
    assert notifications(client, ADMIN, admin=True) == []
    update_service(client, service_id, {"$set": problem})
    assert {item["type"] for item in notifications(client, ADMIN, admin=True)} == {
        "pending-service",
        "environment-not-allowed",
    }


def test_dismissals_of_owners_stay_when_admins_look(client: FlaskClient) -> None:
    """Admins looking doesn't bring back what owners dismissed."""
    service_id = register(client)
    fail(client, service_id)
    assert dismiss(client, notifications(client)[0]["id"]) == 200
    age_dismissals(client)
    assert len(notifications(client, ADMIN, admin=True)) == 1
    assert notifications(client) == []
    assert len(dismissals(client)) == 1


def test_dismissals_of_owners_forgotten_after_ending(client: FlaskClient) -> None:
    """What owners dismissed is forgotten once it ended, when admins look.

    Only that: what lasts stays dismissed.
    """
    service_id = register(client)
    other_id = register(client, host="other")
    fail(client, service_id)
    fail(client, other_id)
    for item in notifications(client):
        assert dismiss(client, item["id"]) == 200
    recover(client, service_id)
    age_dismissals(client)
    [lasting] = notifications(client, ADMIN, admin=True)
    assert lasting["serviceId"] == other_id
    assert [item["_id"] for item in dismissals(client)] == [
        f"{notifications_module.OWNERS}:{lasting['id']}"
    ]
    assert notifications(client) == []


def test_dismissals_of_deleted_services_forgotten(client: FlaskClient) -> None:
    """What owners dismissed about a deleted service is forgotten, when admins look."""
    service_id = register(client)
    fail(client, service_id)
    assert dismiss(client, notifications(client)[0]["id"]) == 200
    response = client.delete(f"{BASE}/services/{service_id}", headers=make_headers())
    assert response.status_code == 200
    age_dismissals(client)
    assert notifications(client, ADMIN, admin=True) == []
    assert dismissals(client) == []


def test_dismissals_while_admins_look_stay(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dismissals made while admins list notifications aren't forgotten.

    They may be about what began after the listing found what is current.
    """
    original = notifications_module._current
    dismissal_id = f"{notifications_module.OWNERS}:new"

    def dismiss_meanwhile() -> tuple[str, list[dict[str, Any]]]:
        """Find what is current, then let an owner dismiss what began since.

        Returns:
            What `_current()` returns.
        """
        current = original()
        get_collection(notifications_module.DISMISSALS).insert_one(
            {"_id": dismissal_id, "dismissedAt": now()}
        )
        return current

    monkeypatch.setattr(notifications_module, "_current", dismiss_meanwhile)
    assert notifications(client, ADMIN, admin=True) == []
    assert [item["_id"] for item in dismissals(client)] == [dismissal_id]


def test_invalid_url_is_no_scheme_problem(client: FlaskClient) -> None:
    """URLs with an allowed scheme aren't reported, whatever else they have."""
    service_id = register(client)
    update_service(
        client, service_id, {"$set": {"url": "HTTPS://user:pw@tes.example.org/"}}
    )
    assert notifications(client, ADMIN, admin=True) == []


def test_most_recent_first(client: FlaskClient) -> None:
    """The most recent problems come first."""
    older = register(client, "older")
    newer = register(client, "newer")
    fail(client, older, days=5)
    fail(client, newer, days=1)
    assert [item["serviceId"] for item in notifications(client)] == [newer, older]


def test_pending_for_admins(client: FlaskClient) -> None:
    """Admins see pending service types and services; owners don't."""
    service_id = register(client)
    update_service(client, service_id, {"$set": {PENDING_SERVICE: True}})
    pending_type = {"group": "org.ga4gh", "artifact": "tes", "version": "9"}
    with client.application.app_context():
        get_collection("service_types").insert_one(
            {**pending_type, PENDING: True, "createdBy": USER}
        )
    assert notifications(client) == []
    items = {item["type"]: item for item in notifications(client, ADMIN, admin=True)}
    assert set(items) == {"pending-service", "pending-service-type"}
    assert items["pending-service"]["serviceId"] == service_id
    assert items["pending-service"]["reason"] == "awaiting approval"
    assert items["pending-service-type"]["serviceType"] == pending_type
    assert "serviceId" not in items["pending-service-type"]
    assert items["pending-service-type"]["reason"] == "awaiting approval"
    assert (
        dismiss(client, items["pending-service-type"]["id"], ADMIN, admin=True) == 200
    )
    assert {item["type"] for item in notifications(client, ADMIN, admin=True)} == {
        "pending-service"
    }


def test_no_longer_allowed_for_admins(client: FlaskClient) -> None:
    """Admins see services that the configuration no longer allows."""
    service_id = register(client)
    update_service(client, service_id, {"$set": {"environment": "gone"}})
    update_service(client, service_id, {"$set": {"url": "http://tes.example.org/x"}})
    assert notifications(client) == []
    items = {item["type"]: item for item in notifications(client, ADMIN, admin=True)}
    assert set(items) == {"environment-not-allowed", "url-scheme-not-allowed"}
    assert items["environment-not-allowed"]["serviceId"] == service_id
    assert items["environment-not-allowed"]["reason"] == "environment not allowed"
    assert items["url-scheme-not-allowed"]["reason"] == "url scheme not allowed"


@pytest.mark.parametrize("environments", [[], ["prod"]], ids=["any", "allowed"])
def test_allowed_environment(client: FlaskClient, environments: list[str]) -> None:
    """Environments that are allowed are no problem."""
    services_config(client).environments = environments
    service_id = register(client)
    update_service(client, service_id, {"$set": {"environment": "prod"}})
    assert notifications(client, ADMIN, admin=True) == []
