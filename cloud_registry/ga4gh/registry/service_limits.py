"""Limit of the services that a user registers.

Users other than admins may only have registered a limited number of the
registered services (`custom.access_control.max_services_per_user`), so that
a single account can't fill the registry. Services count for the user who
registered them (`CREATED_BY`, stored with the service, not served), as long
as they are registered, whatever their roles later (e.g., if the user gives
the service to a group and leaves it); renames keep it, and services that
admins register (in admin mode) count for nobody. Only `POST /services`
registers services for users other than admins (`PUT /services/{serviceId}`
creates services for admins only), so updates, renames, roles and accepted
invitations never fail on the limit.

The number of services each user registered is kept in a counter per user
(collection `SERVICE_COUNTS`, by user identifier), so that the limit holds
also for concurrent registrations without transactions (which MongoDB only
supports on replica sets): each registration reserves one with a single
atomic conditional increment before the service is stored (cf.
`reserve_service()`), and releases it if the service isn't stored;
deletions release it (cf. `release_service()`). The counters are kept also
without a limit, so that one can be set later.

A counter can drift if the app stops between storing (or deleting) a service
and updating it, or if a registration or deletion runs while the app starts
on another instance; the app reconciles the counters with the stored services
on startup (cf. `reconcile_service_counts()`), after adding the user to
services registered before it was stored (cf. `add_service_creators()`).
"""

import logging
from typing import Any

from flask import current_app
from foca import get_custom_config
from foca.security.access_control import list_resource_roles
from foca.utils.logging import escape_log_value
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import CodedConflict
from cloud_registry.ga4gh.registry.service import CREATED_BY
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)
# Collection with the number of services each user registered (`COUNT`),
# by user identifier (`_id`)
SERVICE_COUNTS = "service_counts"
COUNT = "count"
# End of the endpoint of `POST /services` (cf. `services_path_on_startup()`)
POST_SERVICES_ENDPOINT = ".cloud_registry_ga4gh_registry_server_postService"


class ServicesLimit(CodedConflict):
    """A user other than an admin has registered too many services."""

    problem_code = "services-limit"

    def __init__(self, limit: int) -> None:
        """Create the exception, with a description of what to do.

        Args:
            limit: How many services a user may have registered.
        """
        super().__init__(
            f"You have registered {limit} services, as many as allowed; "
            "delete services you no longer need, or ask an admin to register "
            "further ones."
        )


def reserve_service(user: str | None) -> bool:
    """Reserve one of the services a user may register.

    Must be called before the service is stored, in an app context; release
    the reservation if the service isn't stored (cf. `release_service()`).
    The check and the increment are a single write, so concurrent
    registrations of a user can't exceed the limit.

    Args:
        user: User who registers the service; `None` for registrations that
            count for nobody (e.g., by admins in admin mode).

    Returns:
        Whether a service was reserved, i.e., the user is not `None`.

    Raises:
        ServicesLimit: The user has registered as many services as allowed
            (`custom.access_control.max_services_per_user`).
    """
    if user is None:
        return False
    limit = get_custom_config(CustomConfig).access_control.max_services_per_user
    counts = get_collection(SERVICE_COUNTS)
    if limit is None:
        counts.update_one({"_id": user}, {"$inc": {COUNT: 1}}, upsert=True)
        return True
    if limit > 0:
        try:
            # inserts a counter if the user has none, and fails (duplicate
            # key) if the user has one at the limit
            counts.find_one_and_update(
                {"_id": user, COUNT: {"$lt": limit}},
                {"$inc": {COUNT: 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            pass
        else:
            return True
    logger.info(
        "Service not registered: '%s' registered %d services.",
        escape_log_value(user),
        limit,
    )
    raise ServicesLimit(limit)


def release_service(user: str | None) -> None:
    """Release one of the services a user registered.

    For deleted services, and for reservations whose service wasn't stored
    (cf. `reserve_service()`). Counters never become negative.

    Args:
        user: User who registered the service (cf. `CREATED_BY`); `None` for
            services that count for nobody.
    """
    if user is None:
        return
    get_collection(SERVICE_COUNTS).update_one(
        {"_id": user, COUNT: {"$gt": 0}}, {"$inc": {COUNT: -1}}
    )


def services_path_on_startup() -> str:
    """Get the path of the services, under which roles are granted.

    As `cloud_registry.ga4gh.registry.permissions.services_path()`, but
    from the route of `POST /services`, as there is no request on startup.
    Must be called in an app context.

    Returns:
        Path of the service collection, with the base path of the API.

    Raises:
        RuntimeError: The app has no route of `POST /services`.
    """
    for rule in current_app.url_map.iter_rules():
        if rule.endpoint.endswith(POST_SERVICES_ENDPOINT):
            return rule.rule
    raise RuntimeError("The app has no route of 'POST /services'")


def _single_user_owner(path: str) -> str | None:
    """Get the only user with the role `owner` on a service.

    Args:
        path: Path of the service, under which roles are granted.

    Returns:
        The user, if exactly one user (not group) is granted `owner`;
        otherwise `None`.
    """
    owners = [
        subject
        for subject, role in list_resource_roles(path, subject_type="user")
        if role == "owner"
    ]
    return owners[0] if len(owners) == 1 else None


def add_service_creators() -> None:
    """Add the user who registered a service to services without one.

    For services registered before the user was stored (cf. `CREATED_BY`):
    the user who is the only user granted `owner` on the service, usually
    the one who registered it; services with no or several such users count
    for nobody (`None`). Services that already have the field (also `None`)
    are kept, so the function is idempotent and safe to run concurrently
    (e.g., in each worker of the app on startup). Must be called in an app
    context.
    """
    services = get_collection("services")
    path = services_path_on_startup()
    for service in services.find({CREATED_BY: {"$exists": False}}, {"id": True}):
        creator = _single_user_owner(f"{path}/{service['id']}")
        result = services.update_one(
            {"_id": service["_id"], CREATED_BY: {"$exists": False}},
            {"$set": {CREATED_BY: creator}},
        )
        if result.modified_count and creator is not None:
            logger.info(
                "Service '%s' counts for its owner '%s' (registration limit).",
                escape_log_value(service["id"]),
                escape_log_value(creator),
            )


def reconcile_service_counts() -> None:
    """Set the counters of services per user to the stored services.

    Corrects counters that drifted (cf. the module's description). Each
    counter is only set if it didn't change since it was read, so
    registrations and deletions while the app starts are not overwritten;
    they may still make the counter differ by one each, until the next
    start. Must be called in an app context, after `add_service_creators()`.
    """
    counts = get_collection(SERVICE_COUNTS)
    stored: dict[str, int] = {
        counter["_id"]: counter.get(COUNT, 0) for counter in counts.find({})
    }
    actual: dict[str, int] = {
        group["_id"]: group["n"]
        for group in get_collection("services").aggregate(
            [
                {"$match": {CREATED_BY: {"$type": "string"}}},
                {"$group": {"_id": f"${CREATED_BY}", "n": {"$sum": 1}}},
            ]
        )
    }
    for user in stored.keys() | actual.keys():
        before, after = stored.get(user), actual.get(user, 0)
        if before == after:
            continue
        query: dict[str, Any] = {"_id": user}
        query[COUNT] = before if before is not None else {"$exists": False}
        try:
            result = counts.update_one(query, {"$set": {COUNT: after}}, upsert=True)
        except DuplicateKeyError:
            # changed or added concurrently
            continue
        if result.modified_count or result.upserted_id is not None:
            logger.info(
                "Number of services registered by '%s' corrected from %s to %d.",
                escape_log_value(user),
                before if before is not None else "none",
                after,
            )
