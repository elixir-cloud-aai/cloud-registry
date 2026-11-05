"""Notifications: what needs the attention of the current user.

There is no inbox per user: notifications are computed from the stored data when
they are read, so that they end with what caused them, and nothing needs to be
sent or cleaned up. Users see the problems of syncs of the services they have a
role on; admins see those of all services, and also pending approvals and services
that the configuration no longer allows.

Only dismissals are stored, per audience: any user with a role on a service
dismisses a notification for all of them, and any admin for all admins, but not
for each other. The identifier of a notification stands for one episode of a
problem (cf. `_notification_id()`), so that a dismissed notification only comes back
if the problem ended and came back, or its reason changed. Dismissals don't refer to
users, and can only be made by human actions, so they stay.
"""

from collections.abc import Iterator
from enum import StrEnum
import hashlib
import logging
from typing import Any

from foca import get_custom_config
from foca.security.access_control import resource_filter
from foca.utils.logging import escape_log_value

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.permissions import (
    current_user,
    services_path,
)
from cloud_registry.ga4gh.registry.service import (
    PENDING_SERVICE,
    SYNC,
    UPDATED,
    InvalidService,
    check_environment,
    check_url_scheme,
)
from cloud_registry.ga4gh.registry.service_types import PENDING, TYPE_FIELDS
from cloud_registry.ga4gh.registry.sync import (
    Status,
    sync_enabled,
    sync_status,
)
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import format_time, get_collection, now

logger = logging.getLogger(__name__)

# Collection of the dismissals, by audience and identifier of the notification
DISMISSALS = "notification_dismissals"
# Who dismisses a notification for whom
OWNERS = "owners"
ADMINS = "admins"
# Length of identifiers of notifications, in hexadecimal digits
ID_LENGTH = 32


class NotificationType(StrEnum):
    """Type of a notification."""

    OUT_OF_SYNC = "out-of-sync"
    PENDING_SERVICE_TYPE = "pending-service-type"
    PENDING_SERVICE = "pending-service"
    ENVIRONMENT = "environment-not-allowed"
    URL_SCHEME = "url-scheme-not-allowed"


# Reason of notifications about services without a reason of a sync: syncs have
# failed to happen for too long (cf. `sync_status()`)
NO_RECENT_SYNC = "no recent sync"
# Reasons of the other types
AWAITING_APPROVAL = "awaiting approval"
ENVIRONMENT_NOT_ALLOWED = "environment not allowed"
URL_SCHEME_NOT_ALLOWED = "url scheme not allowed"


def _notification_id(kind: NotificationType, subject: str, episode: str) -> str:
    """Get the identifier of a notification.

    Args:
        kind: Type.
        subject: What it is about, stable across renames.
        episode: What stays the same as long as this problem lasts, with the same
            reason.

    Returns:
        Identifier.
    """
    digest = hashlib.sha256(f"{kind}\0{subject}\0{episode}".encode())
    return digest.hexdigest()[:ID_LENGTH]


def _service_notification(
    kind: NotificationType,
    service: dict[str, Any],
    reason: str,
    episode: str,
    since: str | None,
) -> dict[str, Any]:
    """Get a notification about a service.

    Args:
        kind: Type.
        service: Stored service, with `_id` and `id`.
        reason: Reason.
        episode: See `_notification_id()`.
        since: When it started, as served; `None` if unknown.

    Returns:
        Notification.
    """
    notification = {
        "id": _notification_id(kind, str(service["_id"]), episode),
        "type": str(kind),
        "serviceId": service["id"],
        "reason": reason,
    }
    if since is not None:
        notification["since"] = since
    return notification


def _out_of_sync(query: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Get the notifications about services that are out of sync.

    Args:
        query: Filter for the services.

    Yields:
        Notifications; none if the deployment doesn't sync.
    """
    if not sync_enabled():
        return
    current = now()
    for service in get_collection("services").find(
        {SYNC: {"$exists": True}, **query}, {"id": True, SYNC: True}
    ):
        state = service[SYNC]
        if sync_status(state, current) is not Status.OUT_OF_SYNC:
            continue
        reason = state.get("reason", NO_RECENT_SYNC)
        started = (
            state.get("failingSince")
            or state.get("lastSuccessAt")
            or state.get("lastAttemptAt")
        )
        yield _service_notification(
            NotificationType.OUT_OF_SYNC,
            service,
            reason,
            f"{reason}\0{started}",
            format_time(started) if started else None,
        )


def _pending_types() -> Iterator[dict[str, Any]]:
    """Get the notifications about pending service types.

    Yields:
        Notifications.
    """
    for item in get_collection("service_types").find({PENDING: True}):
        yield {
            "id": _notification_id(
                NotificationType.PENDING_SERVICE_TYPE, str(item["_id"]), ""
            ),
            "type": str(NotificationType.PENDING_SERVICE_TYPE),
            "reason": AWAITING_APPROVAL,
            "serviceType": {field: item[field] for field in TYPE_FIELDS},
        }


def _stored_service_problems() -> Iterator[dict[str, Any]]:
    """Get the notifications about pending services, and what's no longer allowed.

    The configuration may have changed since services were stored; they are still
    served then, but can only be updated with allowed values.

    Yields:
        Notifications.
    """
    services_config = get_custom_config(CustomConfig).endpoints.services
    for service in get_collection("services").find(
        {},
        {
            "id": True,
            "url": True,
            "environment": True,
            UPDATED: True,
            PENDING_SERVICE: True,
        },
    ):
        updated = service.get(UPDATED)
        episode = str(updated)
        if service.get(PENDING_SERVICE) is True:
            yield _service_notification(
                NotificationType.PENDING_SERVICE,
                service,
                AWAITING_APPROVAL,
                episode,
                updated,
            )
        if services_config.environments and "environment" in service:
            try:
                check_environment(service, environments=services_config.environments)
            except InvalidService:
                yield _service_notification(
                    NotificationType.ENVIRONMENT,
                    service,
                    ENVIRONMENT_NOT_ALLOWED,
                    episode,
                    updated,
                )
        try:
            check_url_scheme(service["url"], schemes=services_config.url_schemes)
        except InvalidService:
            yield _service_notification(
                NotificationType.URL_SCHEME,
                service,
                URL_SCHEME_NOT_ALLOWED,
                episode,
                updated,
            )


def _current() -> tuple[str, list[dict[str, Any]]]:
    """Get what needs the attention of the current user, dismissed or not.

    Must be called in a request context.

    Returns:
        Audience of the user, and their notifications.
    """
    own = resource_filter(services_path())
    if own:
        return OWNERS, list(_out_of_sync(own))
    return ADMINS, [
        *_out_of_sync({}),
        *_pending_types(),
        *_stored_service_problems(),
    ]


def _dismissal_id(audience: str, notification_id: str) -> str:
    """Get the identifier of a dismissal.

    Args:
        audience: Audience.
        notification_id: Identifier of the notification.

    Returns:
        Identifier of the dismissal.
    """
    return f"{audience}:{notification_id}"


# GET /notifications
def getNotifications(**kwargs: Any) -> list[dict[str, Any]]:
    """List what needs the attention of the current user.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Notifications that the audience of the user hasn't dismissed, most recent
        first (those without a time last), then sorted by identifier.
    """
    audience, notifications = _current()
    dismissed = {
        item["_id"]
        for item in get_collection(DISMISSALS).find(
            {
                "_id": {
                    "$in": [
                        _dismissal_id(audience, item["id"]) for item in notifications
                    ]
                }
            },
            {"_id": True},
        )
    }
    remaining = [
        item
        for item in sorted(notifications, key=lambda item: item["id"])
        if _dismissal_id(audience, item["id"]) not in dismissed
    ]
    return sorted(remaining, key=lambda item: item.get("since", ""), reverse=True)


# DELETE /notifications/{notificationId}
def deleteNotification(notificationId: str, **kwargs: Any) -> str:
    """Dismiss a notification, for the audience of the current user.

    Args:
        notificationId: Identifier of the notification.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier of the notification.

    Raises:
        cloud_registry.exceptions.NotFound: The user has no such notification (it
            may have ended, or been dismissed already).
    """
    audience, notifications = _current()
    if notificationId not in {item["id"] for item in notifications}:
        raise NotFound
    dismissal = _dismissal_id(audience, notificationId)
    collection = get_collection(DISMISSALS)
    if collection.find_one({"_id": dismissal}, {"_id": True}) is not None:
        raise NotFound
    collection.update_one(
        {"_id": dismissal}, {"$setOnInsert": {"dismissedAt": now()}}, upsert=True
    )
    logger.info(
        "Notification '%s' dismissed by '%s'.",
        escape_log_value(notificationId),
        current_user(),
    )
    return notificationId
