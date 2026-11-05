"""Syncing services with their service info, and the label that shows it.

Owners and maintainers of a service turn sync on (`PUT
/services/{serviceId}/sync`); the app then fetches the service info of the
registered URL (`{url}/service-info`) periodically, with the safeguards of
`cloud_registry.ga4gh.registry.service_import`, and applies it: descriptive
fields and `type` (cf. `SYNCED_FIELDS`), never the identifier, the URL or the
roles. The verification stays. If a sync fails, the last values stay, and
`Reason` says why, from a fixed set: no network errors are ever served.

The state is an internal field of the service (`SYNC`), not served with it, so
that the GA4GH endpoints are unchanged, renames keep it and deleting a service
deletes it. The label is computed when it is read (cf. `sync_status()`): only
a recent success counts, so that a scheduler that stopped can't show `synced`
forever.

Sync is turned on only after a successful fetch of the URL as stored, so that
nobody can read the reason for an address of their choice right away: as for
imports, all failed fetches look alike to the user (cf. `FetchFailed`). The
reasons of later syncs are served, though, so whoever controls a host name can
still point it elsewhere and learn something about the target, slowly: one probe
per service and interval (see "Service info sync" in `docs/configuration.md`).

The scheduler is a daemon thread in each worker (cf. `start_sync_scheduler()`),
so it needs neither a task queue nor incoming requests. Workers and replicas
coordinate through the database: a due service is claimed in a single write
moving its next attempt ahead (cf. `_claim()`), which costs one indexed query per
tick with a single worker, and the result is only written if the claim and the
service are unchanged, so that any change meanwhile wins (cf. `_write()`); the
sync is then tried again when the claim expires. All state is in the
database, so that restarts lose nothing: overdue services are synced at the first
tick, and those of a worker that died when their claim expires.
"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import random
import threading
from typing import Any, cast

from flask import Flask, Response
from foca import get_custom_config
from foca.utils.logging import escape_log_value
from pymongo import ReturnDocument

from cloud_registry.exceptions import (
    CodedConflict,
    NotFound,
    ServiceBusy,
    TooManyRequests,
    problem,
)
from cloud_registry.ga4gh.registry.locks import changing
from cloud_registry.ga4gh.registry.permissions import (
    check_read_token,
    current_user,
    require_subject,
)
from cloud_registry.ga4gh.registry.service import (
    CREATED_BY,
    KEY,
    PENDING_SERVICE,
    SELF_ENTRY,
    SYNC,
    UPDATED,
    InvalidService,
    normalize_url,
    service_key,
)
from cloud_registry.ga4gh.registry.service_import import (
    OBJECT_FIELDS,
    SERVICE_FIELDS,
    SERVICE_INFO_PATH,
    FetchFailed,
    check_fetch_limit,
    fetch_service_info,
    fetch_slot,
    service_values,
)
from cloud_registry.ga4gh.registry.service_types import (
    SyncedType,
    decide_synced_type,
    sync_pending_flag,
    type_document,
    visible_services,
)
from cloud_registry.safe_fetch import AddressNotAllowed, FetchError, Unreachable
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import format_time, get_collection, now

logger = logging.getLogger(__name__)


class Status(StrEnum):
    """Label of a service with sync turned on."""

    SYNCED = "synced"
    OUT_OF_SYNC = "out-of-sync"


class Reason(StrEnum):
    """Why a service is out of sync; served, so a fixed set."""

    UNREACHABLE = "unreachable"
    ADDRESS_NOT_ALLOWED = "address not allowed"
    INVALID = "invalid service info"
    AWAITING_APPROVAL = "version awaiting approval"
    VERSION_NOT_ALLOWED = "version not allowed"
    TYPES_LIMIT = "unused types limit reached"
    IDENTITY_MISMATCH = "identity mismatch"


# Reasons of types that can't be applied (cf. `decide_synced_type()`)
TYPE_REASONS = {
    SyncedType.PENDING: Reason.AWAITING_APPROVAL,
    SyncedType.NOT_ALLOWED: Reason.VERSION_NOT_ALLOWED,
    SyncedType.LIMIT: Reason.TYPES_LIMIT,
}
# Fields of a service that syncs set, and those they remove if the service info
# doesn't have them (the others are required)
SYNCED_FIELDS = (*SERVICE_FIELDS, *OBJECT_FIELDS)
OPTIONAL_FIELDS = ("description", "contactUrl", "documentationUrl", "environment")
# Field of the state with the time of the next attempt, also holding claims
NEXT_ATTEMPT = "nextAttemptAt"
NEXT = f"{SYNC}.{NEXT_ATTEMPT}"
# Time for which a claim of a service keeps others from syncing it: more than a
# fetch can take, so a claim only expires if its worker died
CLAIM = timedelta(minutes=5)
# How often a sync of an unreachable service is tried again in a cycle before it
# counts as failed, and how long to wait (twice as long the second time, but at
# most an interval)
RETRIES = 2
RETRY_WAIT = timedelta(minutes=5)
# Longest time between checks for due services, in seconds
TICK = 60.0
# Label is `synced` only if the last success is at most so many intervals ago
MAX_INTERVALS = 2


class SyncUnavailable(CodedConflict):
    """Sync can't be turned on: not in this deployment, or not for this entry."""

    problem_code = "sync-unavailable"


class SyncIdentityMismatch(CodedConflict):
    """The service info at the URL describes another service."""

    problem_code = "sync-identity-mismatch"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "The service info has another identifier or API type (group, artifact) "
            "than this service. To accept another identifier, turn sync off first."
        )


def sync_enabled() -> bool:
    """Check whether the deployment syncs services.

    Must be called in an app context.

    Returns:
        Whether sync is on (`custom.endpoints.services.service_info.sync`).
    """
    services = get_custom_config(CustomConfig).endpoints.services
    return services.service_info.sync


def sync_interval() -> timedelta:
    """Get the time between syncs of a service.

    Must be called in an app context.

    Returns:
        Interval (`custom.endpoints.services.service_info.sync_interval_hours`).
    """
    services = get_custom_config(CustomConfig).endpoints.services
    return timedelta(hours=services.service_info.sync_interval_hours)


def _aware(time: datetime) -> datetime:
    """Make a time comparable, as times read from the database are naive.

    Args:
        time: Time in UTC.

    Returns:
        The time, with its time zone.
    """
    return time if time.tzinfo else time.replace(tzinfo=UTC)


def sync_status(state: dict[str, Any], current: datetime | None = None) -> Status:
    """Get the label of a service.

    Args:
        state: Sync state of the service (cf. `SYNC`).
        current: Current time; by default, now.

    Returns:
        `synced` if the last attempt that counts didn't fail, and the last
        success is at most `MAX_INTERVALS` intervals ago; otherwise,
        `out-of-sync`.
    """
    success = state.get("lastSuccessAt")
    if "reason" in state or success is None:
        return Status.OUT_OF_SYNC
    age = (current or now()) - _aware(success)
    return (
        Status.SYNCED if age <= MAX_INTERVALS * sync_interval() else Status.OUT_OF_SYNC
    )


def _view(service: dict[str, Any], current: datetime) -> dict[str, Any]:
    """Get the sync state of a stored service, as served.

    Args:
        service: Stored service, with `url`, `type` and `SYNC`.
        current: Current time.

    Returns:
        URL and API type of the service, its label (`status`), the identifier of
        its service info and the times of the last attempt and success, and the
        reason if it has one (a label `out-of-sync` without one means that no
        attempt failed, but none succeeded in time: syncs aren't running).
    """
    state = service[SYNC]
    view = {
        "url": service["url"],
        "type": service["type"],
        "status": str(sync_status(state, current)),
        "serviceInfoId": state["serviceInfoId"],
    }
    if "reason" in state:
        view["reason"] = state["reason"]
    for field in ("lastAttemptAt", "lastSuccessAt"):
        if state.get(field) is not None:
            view[field] = format_time(state[field])
    return view


def _fetch(url: str) -> tuple[str, dict[str, Any]]:
    """Fetch the service info of a service.

    Args:
        url: URL of the service, as registered.

    Returns:
        Identifier of the service info, and its values for the service.

    Raises:
        cloud_registry.safe_fetch.FetchError: The fetch failed.
        cloud_registry.ga4gh.registry.service.InvalidService: The URL or the
            service info is invalid.
    """
    service_info = fetch_service_info(f"{normalize_url(url)}{SERVICE_INFO_PATH}")
    values = service_values(service_info, url)
    return cast(dict[str, Any], service_info)["id"], values


def _write(
    service: dict[str, Any],
    state: dict[str, Any],
    drop: tuple[str, ...] = (),
    fields: dict[str, Any] | None = None,
    removed: tuple[str, ...] = (),
) -> bool:
    """Write the result of a sync, unless the service changed since the claim.

    Whatever a user changes renews `updatedAt`, and approvals change the pending
    flag; the results of the fetch may no longer fit then.

    Args:
        service: Stored service, as claimed.
        state: Fields of the sync state to set.
        drop: Fields of the sync state to remove.
        fields: Fields of the service to set.
        removed: Fields of the service to remove.

    Returns:
        Whether the result was written.
    """
    unset = {**dict.fromkeys(removed, ""), **{f"{SYNC}.{key}": "" for key in drop}}
    update: dict[str, Any] = {
        "$set": {**(fields or {}), **{f"{SYNC}.{k}": v for k, v in state.items()}}
    }
    if unset:
        update["$unset"] = unset
    query = {"_id": service["_id"], NEXT: service[SYNC][NEXT_ATTEMPT]}
    for field in ("url", KEY, UPDATED, PENDING_SERVICE):
        query[field] = service.get(field, {"$exists": False})
    result = get_collection("services").update_one(query, update)
    return result.modified_count > 0


def _failed(service: dict[str, Any], reason: Reason, current: datetime) -> None:
    """Record a failed sync; the service keeps its values.

    Args:
        service: Stored service, as claimed.
        reason: Reason.
        current: Current time.
    """
    state = service[SYNC]
    _write(
        service,
        {
            "lastAttemptAt": current,
            "reason": str(reason),
            # the time the problem started, whatever the reasons since
            "failingSince": state.get("failingSince", current),
            "retries": 0,
            NEXT_ATTEMPT: current + sync_interval(),
        },
    )
    logger.info(
        "Service '%s' out of sync: %s.", escape_log_value(service["id"]), reason
    )


def _unreachable(service: dict[str, Any], current: datetime) -> None:
    """Record that a service couldn't be reached.

    Only counts as failed after the retries of a cycle (cf. `RETRIES`), so that
    a short interruption doesn't change the label.

    Args:
        service: Stored service, as claimed.
        current: Current time.
    """
    retries = service[SYNC].get("retries", 0)
    if retries >= RETRIES:
        _failed(service, Reason.UNREACHABLE, current)
        return
    wait = min(RETRY_WAIT * (retries + 1), sync_interval())
    _write(
        service,
        {
            "lastAttemptAt": current,
            "retries": retries + 1,
            NEXT_ATTEMPT: current + wait,
        },
    )


def _reason(
    service: dict[str, Any], identifier: str, values: dict[str, Any]
) -> Reason | None:
    """Check whether service info can be applied to a service.

    Adds a new type if it may be added (cf. `decide_synced_type()`).

    Args:
        service: Stored service, with its sync state.
        identifier: Identifier of the service info.
        values: Values of the service info, valid for the service.

    Returns:
        Why they can't be applied, or `None`.
    """
    # the key has the URL too, which is the same
    if (
        identifier != service[SYNC]["serviceInfoId"]
        or service_key(values) != service[KEY]
    ):
        return Reason.IDENTITY_MISMATCH
    service_type = cast(dict[str, str], type_document(values["type"]))
    # services of admins count for nobody, so each has its own limit
    counted_as = service.get(CREATED_BY) or f"service:{service['_id']}"
    return TYPE_REASONS.get(decide_synced_type(service_type, counted_as))


def _apply(
    service: dict[str, Any], identifier: str, values: dict[str, Any], current: datetime
) -> None:
    """Apply fetched service info to a service, or record why it can't be.

    `updatedAt` only changes if a value does.

    Args:
        service: Stored service, as claimed.
        identifier: Identifier of the service info.
        values: Values of the service info, valid for the service.
        current: Current time.
    """
    reason = _reason(service, identifier, values)
    if reason is not None:
        _failed(service, reason, current)
        return
    fields = {
        field: values[field]
        for field in SYNCED_FIELDS
        if field in values and service.get(field) != values[field]
    }
    removed = tuple(
        field for field in OPTIONAL_FIELDS if field in service and field not in values
    )
    if fields or removed:
        fields[UPDATED] = format_time(current)
    written = _write(
        service,
        {
            "lastAttemptAt": current,
            "lastSuccessAt": current,
            "retries": 0,
            NEXT_ATTEMPT: current + sync_interval(),
        },
        drop=("reason", "failingSince"),
        fields=fields,
        removed=removed,
    )
    if written and PENDING_SERVICE in service:
        # the service may have left a pending type for an approved one
        sync_pending_flag(service["id"], cast(dict[str, str], values["type"]))


def sync_service(service: dict[str, Any]) -> None:
    """Sync a claimed service with its service info.

    Must be called in an app context.

    Args:
        service: Stored service, as claimed (cf. `_claim()`).
    """
    try:
        identifier, values = _fetch(service["url"])
    except AddressNotAllowed:
        _failed(service, Reason.ADDRESS_NOT_ALLOWED, now())
    except Unreachable:
        _unreachable(service, now())
    except InvalidService:
        _failed(service, Reason.INVALID, now())
    else:
        _apply(service, identifier, values, now())


def _claim() -> dict[str, Any] | None:
    """Claim the service most overdue for a sync, if any.

    The claim moves its next attempt ahead, so that no other worker or replica
    syncs it, unless this worker dies before writing the result.

    Returns:
        The service, as claimed, or `None`.
    """
    current = now()
    return get_collection("services").find_one_and_update(
        {NEXT: {"$lte": current}},
        {"$set": {NEXT: current + CLAIM}},
        sort=[(NEXT, 1)],
        return_document=ReturnDocument.AFTER,
    )


def run_due_syncs() -> int:
    """Sync all services that are due.

    Must be called in an app context. Failures of a service are logged, so that
    they don't keep others from being synced; it is tried again when its claim
    expires.

    Returns:
        Number of services claimed.
    """
    count = 0
    while (service := _claim()) is not None:
        count += 1
        try:
            sync_service(service)
        except Exception:
            logger.exception(
                "Service '%s' not synced; tried again later.",
                escape_log_value(service["id"]),
            )
    return count


def cap_next_attempts() -> None:
    """Bring next attempts forward to at most an interval from now.

    Run on startup, so that a shorter interval applies at once (in-flight
    claims are at most moved ahead by less than they would be otherwise).
    Idempotent, and safe to run concurrently (e.g., in each worker).
    """
    if sync_enabled():
        latest = now() + sync_interval()
        get_collection("services").update_many(
            {NEXT: {"$gt": latest}}, {"$set": {NEXT: latest}}
        )


def _run_scheduler(app: Flask, stop: threading.Event) -> None:
    """Sync due services until told to stop.

    Errors (e.g., while the database is unavailable) are logged, and tried again
    at the next tick. The first tick comes after a random time, so that workers
    and replicas started together don't all query at once.

    Args:
        app: App.
        stop: Event ending the loop.
    """
    with app.app_context():
        tick = min(TICK, max(sync_interval().total_seconds(), 1.0))
    delay = random.uniform(0, tick)  # noqa: S311 (not for security)
    while not stop.wait(delay):
        try:
            with app.app_context():
                run_due_syncs()
        except Exception:
            logger.exception("Syncs of services failed; tried again later.")
        delay = tick


def start_sync_scheduler(app: Flask) -> threading.Thread | None:
    """Start the scheduler of syncs in this worker, if the deployment syncs.

    Must be called in an app context, after the worker has forked, as database
    clients and threads don't survive forks (the image doesn't preload the app).

    Args:
        app: App.

    Returns:
        The thread of the scheduler, a daemon, which ends with the worker; `None`
        if the deployment doesn't sync.
    """
    if not sync_enabled():
        return None
    thread = threading.Thread(
        target=_run_scheduler,
        args=(app, threading.Event()),
        daemon=True,
        name="service-sync",
    )
    thread.start()
    return thread


# GET /services/{serviceId}/sync
@check_read_token
def getServiceSync(serviceId: str, **kwargs: Any) -> dict[str, Any]:
    """Get the sync state of a service.

    Args:
        serviceId: Service identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Sync state of the service (cf. `_view()`).

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, has no
            sync, is pending and the user may not see it, or the deployment
            doesn't sync.
    """
    service = None
    if sync_enabled():
        service = get_collection("services").find_one(
            {"id": serviceId, SYNC: {"$exists": True}, **visible_services()},
            {"_id": False, "url": True, "type": True, SYNC: True},
        )
    if service is None:
        raise NotFound
    return _view(service, now())


# GET /syncs
@check_read_token
def getSyncs(status: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """List the sync states of services.

    Args:
        status: Only list services with this label.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier and sync state of each service with sync, sorted by
        identifier; without the pending services that the user may not see. None
        if the deployment doesn't sync.
    """
    if not sync_enabled():
        return []
    current = now()
    services = get_collection("services").find(
        {SYNC: {"$exists": True}, **visible_services()},
        {"_id": False, "id": True, "url": True, "type": True, SYNC: True},
        sort=[("id", 1)],
    )
    views = ({"serviceId": item["id"], **_view(item, current)} for item in services)
    return [view for view in views if status is None or view["status"] == status]


# PUT /services/{serviceId}/sync
def putServiceSync(serviceId: str, **kwargs: Any) -> dict[str, Any] | Response:
    """Turn sync of a service on, or sync it now.

    Sync is only turned on after a successful fetch, so that users can't read the
    reasons for addresses of their choice. The fetch counts for the limit of the
    user (cf. `check_fetch_limit()`).

    Args:
        serviceId: Service identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Sync state of the service, also if the service info can't be applied (cf.
        `Reason`); or an error response, changing nothing: `400` if the service
        info is invalid, `409` if it describes another service, the deployment
        doesn't sync, the service is the registry's own entry or it changed during
        the fetch, `429` above the limits for fetches, and `502` if the fetch
        fails, for any reason.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist (anymore),
            or sync was turned off meanwhile.
    """
    collection = get_collection("services")
    with changing(serviceId):
        service = collection.find_one({"id": serviceId})
        if service is None:
            raise NotFound
        if not sync_enabled() or SELF_ENTRY in service:
            return problem(
                SyncUnavailable(
                    "This deployment doesn't sync services."
                    if not sync_enabled()
                    else "The registry's own entry follows its service info."
                )
            )
        try:
            with fetch_slot():
                check_fetch_limit(require_subject())
                identifier, values = _fetch(service["url"])
        except FetchError as exc:
            logger.info(
                "Sync of service '%s' not started by '%s': %s.",
                escape_log_value(serviceId),
                current_user(),
                type(exc).__name__,
            )
            return problem(FetchFailed())
        except (TooManyRequests, InvalidService) as exc:
            return problem(exc)
        state = service.get(SYNC, {})
        baseline = state.get("serviceInfoId", identifier)
        if identifier != baseline or service_key(values) != service[KEY]:
            return problem(SyncIdentityMismatch())
        current = now()
        # also not if the URL changed during the fetch, as it wasn't fetched then
        claimed = collection.find_one_and_update(
            {
                "_id": service["_id"],
                "url": service["url"],
                KEY: service[KEY],
                SYNC: service.get(SYNC, {"$exists": False}),
            },
            {
                "$set": {
                    SYNC: {
                        **state,
                        "serviceInfoId": baseline,
                        NEXT_ATTEMPT: current + CLAIM,
                    }
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if claimed is None:
            if collection.count_documents({"_id": service["_id"]}) == 0:
                raise NotFound
            return problem(ServiceBusy())
        _apply(claimed, identifier, values, current)
        stored = collection.find_one({"_id": service["_id"]})
    if stored is None or SYNC not in stored:
        raise NotFound
    logger.info(
        "Service '%s' synced by '%s'.", escape_log_value(serviceId), current_user()
    )
    return _view(stored, now())


# DELETE /services/{serviceId}/sync
def deleteServiceSync(serviceId: str, **kwargs: Any) -> str:
    """Turn sync of a service off; it keeps its values, and can then change URL.

    Args:
        serviceId: Service identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier of the service.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist or has no
            sync.
    """
    result = get_collection("services").update_one(
        {"id": serviceId, SYNC: {"$exists": True}}, {"$unset": {SYNC: ""}}
    )
    if not result.matched_count:
        raise NotFound
    logger.info(
        "Sync of service '%s' turned off by '%s'.",
        escape_log_value(serviceId),
        current_user(),
    )
    return serviceId
