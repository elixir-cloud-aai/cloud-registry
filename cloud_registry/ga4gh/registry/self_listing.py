"""Controllers for listing the registry among its own services.

Admins can add the registry to its own list of services, or remove it, so
that clients find it like any other service (`PUT` and `DELETE
/service-info/listing`; `GET` tells whether it is listed). Only admins: no
policy in `config.yaml` allows these operations to other users.

The entry is a service like any other, built from the service info and the
public URL of the app, and marked by an internal field (`SELF_ENTRY`), not
served, which it keeps when it is updated (`PUT /services/{serviceId}`) or
renamed. It counts for nobody (`CREATED_BY`), nobody is granted a role on it,
and it is verified (`VERIFIED`) from the start. While it is listed, the app
brings it up to date with the service info on startup (cf.
`refresh_self_listing()`), which also undoes changes of its fields by admins;
only its identifier stays, so that links to it keep working.

Listing the registry stores two documents, the service type and the entry,
without transactions (which need a replica set), in this order:

1. The type, unless it is stored. It is an approved type, as admins add it;
   if it is stored as pending, the operation is refused, as pending services
   can't be verified, and approving it would approve the services of other
   users that have it.
2. The entry, in a single write. Failures remove the type again, if the
   operation added it and no service has it.
3. The type again, in case it was removed concurrently (cf.
   `deleteServiceType()`).

So no entry is left without its type, unless the app stops or the database
fails in step 3, which the app corrects on startup (cf. `add_service_types()`);
at worst, an interruption leaves a type that no service has. The operation
can be repeated. Removing the registry revokes roles on the entry first, so
that it can be repeated if that fails, then deletes the entry, and then its
type, unless services have it (cf. `remove_service_type()`).

The identifier of the entry can change (renames), so operations check it again
once they hold the lock of the service (cf. `_check_entry_id()`).
"""

import logging
from typing import Any

from flask import Response
from foca import get_config
from foca.api.service_info import get_service_info
from foca.errors.exceptions import shorten_detail
from foca.utils.logging import escape_log_value
from foca.utils.urls import get_public_url
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import CodedConflict, NotFound, ServiceBusy, problem
from cloud_registry.ga4gh.registry.locks import changing
from cloud_registry.ga4gh.registry.permissions import (
    current_user,
    remove_permissions,
    services_path,
)
from cloud_registry.ga4gh.registry.rename import check_id
from cloud_registry.ga4gh.registry.service import (
    CREATED,
    CREATED_BY,
    KEY,
    ORGANIZATION_FIELDS,
    RESERVED_IDS,
    SELF_ENTRY,
    UPDATED,
    VERIFIED,
    DuplicateService,
    InvalidService,
    RegisterService,
)
from cloud_registry.ga4gh.registry.service_limits import services_path_on_startup
from cloud_registry.ga4gh.registry.service_types import (
    ServiceTypeInUse,
    is_pending,
    remove_service_type,
    store_service_type,
    sync_pending_flag,
    type_document,
)
from cloud_registry.utils import format_time, get_collection, now

logger = logging.getLogger(__name__)
# Fields of the service info that the entry has (cf. `ExternalServiceRegister`
# in `api/additions.openapi.yaml`), besides `url` and its identifier; not the
# timestamps, which the registry sets for its entries (cf. `RegisterService`)
ENTRY_FIELDS = (
    "name",
    "type",
    "description",
    "organization",
    "contactUrl",
    "documentationUrl",
    "environment",
    "version",
)
# Response of `PUT /service-info/listing`: whether and how the registry is
# listed, with status `201` and the URL of the entry if it was added, or an
# error
ListedResponse = dict[str, Any] | tuple[dict[str, Any], int, dict[str, str]] | Response


class PendingSelfListing(CodedConflict):
    """The service type of the registry is pending."""

    problem_code = "pending-service"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "The service type of the registry is pending (see GET /approvals); "
            "approve it, then list the registry."
        )


class SelfIdTaken(CodedConflict):
    """Another service has the identifier of the service info."""

    problem_code = "service-id-taken"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "A service with the identifier of the service info is registered; "
            "rename or delete it, or change `service_info.service.id`."
        )


def _stored_entry() -> dict[str, Any] | None:
    """Get the stored entry of the registry.

    Returns:
        The entry, with internal fields; `None` if the registry isn't listed.
    """
    return get_collection("services").find_one({SELF_ENTRY: True}, {"_id": False})


def _check_entry_id(service_id: str) -> dict[str, Any]:
    """Check that the entry still has an identifier, once it is locked.

    Args:
        service_id: Identifier of the entry, as read before it was locked.

    Returns:
        The stored entry, as read now.

    Raises:
        cloud_registry.exceptions.NotFound: The entry was removed meanwhile.
        cloud_registry.exceptions.ServiceBusy: The entry was renamed meanwhile,
            so that the lock is another one's; the request can be repeated.
    """
    stored = _stored_entry()
    if stored is None:
        raise NotFound
    if stored["id"] != service_id:
        raise ServiceBusy
    return stored


def _listing(service_id: str | None) -> dict[str, Any]:
    """Get the response for the state of the listing.

    Args:
        service_id: Identifier of the entry; `None` if the registry isn't
            listed.

    Returns:
        Whether the registry is listed (`listed`), and if so, the identifier
        of its entry (`serviceId`).
    """
    if service_id is None:
        return {"listed": False}
    return {"listed": True, "serviceId": service_id}


def _entry_data() -> dict[str, Any]:
    """Get the fields of the entry, as the service info has them now.

    Must be called in an app context. The URL is the public URL of the app
    (`server.public_url`, with any path prefix of a reverse proxy) and the
    base path of the API, so that it is the same on startup, without a
    request, as for requests.

    Returns:
        The fields of `ENTRY_FIELDS` that the service info has, and `url`.
        Organizations only have the fields that services may have.
    """
    info = get_service_info()
    data = {field: info[field] for field in ENTRY_FIELDS if field in info}
    data["organization"] = {
        field: info["organization"][field] for field in sorted(ORGANIZATION_FIELDS)
    }
    public_url = str(get_config().server.public_url).rstrip("/")
    base_path = services_path_on_startup().removesuffix("/services")
    data["url"] = f"{public_url}{base_path}"
    return data


def _new_entry_id() -> str:
    """Get the identifier of a new entry: the one of the service info.

    Must be called in a request context.

    Returns:
        Identifier of the entry.

    Raises:
        InvalidService: The identifier can't be used for services, as for
            `PUT /services/{serviceId}` and renames (cf. `check_id()`).
    """
    service_id = str(get_service_info()["id"])
    if not service_id or service_id in RESERVED_IDS or "/" in service_id:
        raise InvalidService(
            "The identifier of the service info (`service_info.service.id`) "
            "can't be used for services: it must not be empty, `.`, `..` or "
            "`types`, or have slashes."
        )
    check_id(service_id)
    return service_id


def _refresh(service: RegisterService, stored: dict[str, Any], key: str) -> bool:
    """Bring the stored entry up to date, in a single write.

    Nothing is written if it is up to date, so that `updatedAt` only changes
    with the entry (and a start of each worker of the app is enough). Fields
    that the service info no longer has are removed; `createdAt` is kept. The
    entry stays verified: the time is renewed only if its URL or API type
    changed (as a verification applies to those), and set again if an admin
    removed it.

    Args:
        service: Service with the fields of the entry (cf. `_entry_data()`).
        stored: Stored entry, as read.
        key: Key of the entry (cf. `service_key()`).

    Returns:
        Whether the entry is stored; `False` if it was removed concurrently.

    Raises:
        DuplicateService: Another service with the same URL and API type was
            registered concurrently.
    """
    data = {field: value for field, value in service.data.items() if field != "id"}
    fields = {
        field: value for field, value in data.items() if stored.get(field) != value
    }
    removed = {
        field: "" for field in ENTRY_FIELDS if field in stored and field not in data
    }
    new_key = stored.get(KEY) != key
    if fields or removed:
        fields[UPDATED] = format_time(now())
    if new_key:
        fields[KEY] = key
    if new_key or "url" in fields or VERIFIED not in stored:
        fields[VERIFIED] = now()
    if not fields:
        return True
    update: dict[str, Any] = {"$set": fields}
    if removed:
        update["$unset"] = removed
    try:
        result = get_collection("services").update_one({SELF_ENTRY: True}, update)
    except DuplicateKeyError:
        # names the other service, if it is still registered
        service.validate()
        raise
    return result.matched_count > 0


def _insert(service: RegisterService, key: str) -> bool:
    """Store a new entry, in a single write, without roles on it.

    Args:
        service: Service with the fields and identifier of the entry.
        key: Key of the entry (cf. `service_key()`).

    Returns:
        Whether the entry was stored; `False` if it was stored concurrently
        (e.g., by a repeated request).

    Raises:
        DuplicateService: Another service with the same URL and API type was
            registered concurrently.
        SelfIdTaken: Another service has the identifier.
    """
    timestamp = format_time(now())
    services = get_collection("services")
    try:
        # insert a new dict, as `insert_one()` adds an `_id` field
        services.insert_one(
            {
                **service.data,
                CREATED: timestamp,
                UPDATED: timestamp,
                KEY: key,
                CREATED_BY: None,
                VERIFIED: now(),
                SELF_ENTRY: True,
            }
        )
    except DuplicateKeyError as exc:
        service.validate()
        if _stored_entry() is None:
            raise SelfIdTaken from exc
        return False
    try:
        # Roles left on the identifier by a deleted service (e.g., if revoking
        # them failed) don't apply to the entry
        remove_permissions(service.data["id"])
    except Exception:
        services.delete_one({SELF_ENTRY: True, "id": service.data["id"]})
        raise
    return True


def _remove_unused_type(service_type: dict[str, str]) -> None:
    """Remove a service type, unless services have it.

    Args:
        service_type: Service type, in its stored form.
    """
    try:
        remove_service_type(service_type)
    except ServiceTypeInUse:
        logger.debug("Service type kept: services have it.")


def _store(service: RegisterService, stored: dict[str, Any] | None, key: str) -> bool:
    """Store the service type and the entry (cf. the module docstring).

    Args:
        service: Service with the fields and identifier of the entry.
        stored: Stored entry, as read; `None` if the registry wasn't listed.
        key: Key of the entry (cf. `service_key()`).

    Returns:
        Whether the operation added the entry.

    Raises:
        DuplicateService: Another service with the same URL and API type was
            registered concurrently.
        PendingSelfListing: The service type of the registry is pending.
        SelfIdTaken: Another service has the identifier.
        cloud_registry.exceptions.NotFound: The entry was removed concurrently.
        cloud_registry.exceptions.ServiceBusy: The entry was still being
            renamed after waiting, or was renamed meanwhile.
    """
    service_id = service.data["id"]
    service_type = type_document(service.data["type"])
    # the service info has the schema of the GA4GH `Service`
    assert service_type is not None  # noqa: S101 (for type checkers)
    created = False
    with changing(service_id):
        if stored is not None:
            stored = _check_entry_id(service_id)
        added = store_service_type(service_type)
        # not rolled back: a type this operation added isn't pending, so it
        # was removed and added again by a user meanwhile
        if is_pending(service_type):
            raise PendingSelfListing
        try:
            if stored is None:
                created = _insert(service, key)
            elif not _refresh(service, stored, key):
                raise NotFound
        except Exception:
            if added:
                _remove_unused_type(service_type)
            raise
        # also if the type was removed concurrently, as a service has it now
        store_service_type(service_type)
        # if it was removed and added again as pending meanwhile
        sync_pending_flag(service_id, service_type)
    return created


# GET /service-info/listing
def getSelfListing(**kwargs: Any) -> dict[str, Any]:
    """Tell whether the registry is listed among its own services.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        State of the listing (cf. `_listing()`).
    """
    stored = _stored_entry()
    return _listing(None if stored is None else stored["id"])


# PUT /service-info/listing
def putSelfListing(**kwargs: Any) -> ListedResponse:
    """Add the registry to its own services, or bring its entry up to date.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        State of the listing (cf. `_listing()`); if the entry was added, with
        status `201` and its URL as `Location`. Or an error response: `400`
        if the environment or URL scheme of the registry isn't allowed for
        services, or its identifier or another field can't be used for them;
        `409` if another service has the same URL and API type (named in
        `serviceId`) or the identifier, or the service type of the registry is
        pending.
    """
    stored = _stored_entry()
    try:
        service_id = _new_entry_id() if stored is None else stored["id"]
        service = RegisterService(_entry_data(), id=service_id)
        created = _store(service, stored, service.validate())
    except (InvalidService, PendingSelfListing, SelfIdTaken) as exc:
        _log_refused(exc)
        return problem(exc)
    except DuplicateService as exc:
        # the entry itself, renamed after it was read
        current = _stored_entry()
        if current is not None and current["id"] == exc.existing_id:
            raise ServiceBusy from exc
        _log_refused(exc)
        return problem(exc, serviceId=exc.existing_id)
    if created:
        logger.info(
            "Registry listed as service '%s' by '%s'.",
            escape_log_value(service_id),
            current_user(),
        )
        location = get_public_url(f"{services_path()}/{service_id}")
        return _listing(service_id), 201, {"Location": location}
    return _listing(service_id)


def _log_refused(exc: InvalidService | CodedConflict) -> None:
    """Log why the registry isn't listed.

    Args:
        exc: Exception with a request-specific description.
    """
    logger.info(
        "Registry not listed: %s",
        escape_log_value(shorten_detail(str(exc.description))),
    )


# DELETE /service-info/listing
def deleteSelfListing(**kwargs: Any) -> dict[str, Any]:
    """Remove the registry from its own services.

    Revokes the roles granted on the entry, deletes it, and then its service
    type, unless services have it, and revokes roles again, as for other
    services (cf. `deleteService()`). If the operation fails or the app stops
    after the entry was deleted, the type may stay, without services; admins
    remove it (`DELETE /services/types`).

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        State of the listing (cf. `_listing()`).

    Raises:
        cloud_registry.exceptions.NotFound: The registry isn't listed.
        cloud_registry.exceptions.ServiceBusy: The entry was still being
            renamed after waiting, or was renamed meanwhile.
    """
    stored = _stored_entry()
    if stored is None:
        raise NotFound
    service_id = stored["id"]
    with changing(service_id):
        _check_entry_id(service_id)
        remove_permissions(service_id)
        deleted = get_collection("services").find_one_and_delete(
            {SELF_ENTRY: True, "id": service_id}, {"_id": False, "type": True}
        )
        if deleted is None:
            raise NotFound
        service_type = type_document(deleted.get("type"))
        if service_type is not None:
            _remove_unused_type(service_type)
        # a no-op unless a role was granted during the deletion
        remove_permissions(service_id)
    logger.info(
        "Registry (service '%s') no longer listed, by '%s'.",
        escape_log_value(service_id),
        current_user(),
    )
    return _listing(None)


def refresh_self_listing() -> None:
    """Bring the entry of the registry up to date with the service info.

    Run on startup, before the types of the registered services are stored
    (cf. `add_service_types()`), which stores a new type of the entry.
    Idempotent, and safe to run concurrently (e.g., in each worker of the app):
    the entry is written in one write, only if it differs. Entries that the
    service info no longer fits (e.g., a URL scheme that isn't allowed anymore,
    or the URL and API type of another service) are kept as they are, with a
    warning, as the rest of the registry works without them. Must be called
    in an app context.
    """
    stored = _stored_entry()
    if stored is None:
        return
    service = RegisterService(_entry_data(), id=stored["id"])
    try:
        key = service.validate()
        if is_pending(type_document(service.data["type"])):
            raise PendingSelfListing
        _refresh(service, stored, key)
    except (InvalidService, DuplicateService, PendingSelfListing) as exc:
        logger.warning(
            "Entry of the registry among its services not updated: %s",
            escape_log_value(shorten_detail(str(exc.description))),
        )
    except DuplicateKeyError as exc:
        logger.warning(
            "Entry of the registry among its services not updated: %s",
            escape_log_value(exc),
        )
