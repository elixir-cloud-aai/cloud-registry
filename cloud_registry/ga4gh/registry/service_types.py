"""Controllers and helpers for service types.

Service types (`type` of services: `group`, `artifact`, `version`) are
stored in their own collection (`service_types`), and listed by
`GET /services/types`. Who may add them is configured
(`custom.access_control.service_types`): in mode `open`, any authenticated
user, explicitly (`POST /services/types`) or by registering a service with a
new type, which is then stored; in mode `controlled`, admins only, and
services of other users must have a stored type. In every mode, only
admins may remove types (`DELETE /services/types`; no policy in
`config.yaml` allows it to other users), and only types that no service
has: users who add types don't own them, as other users' services may have
them. The user who added a type is stored with it (`CREATED_BY`).

Every type of a registered service is stored: registrations store the type
after the service (cf. `store_service_type()`; pending types before it, cf.
`store_pending_type()`), types are only removed if no
service has them, also when a service is registered concurrently, and the
types of services registered before types were stored are added on startup
(cf. `add_service_types()`). A registration that is in progress while an
admin removes its type stores the type again; then it is stored without the
user if they may not add types.

In mode `moderated`, any authenticated user may add types, but the types
that users other than admins add are pending (`PENDING`) until an admin
approves them (`PUT /services/types/approval`); there is no way back, admins
remove types instead. Services that have a pending type are pending too:
derived from their type, and stored with them as an indexed flag
(`PENDING_SERVICE`), so that reads filter by it whatever the number of
pending types; registrations set it, approvals clear it, and the app
reconciles it on startup (cf. `sync_pending_services()`). Pending types are
only listed (`GET /services/types`) to admins and the user who added them,
pending services (`GET /services`, `GET /services/{serviceId}`) to admins
and the users with a role on them; reads without a token have no user,
so they don't list them (cf. `GET /approvals`, which requires login, and
`check_read_token()`). Pending services can't be verified, and lose their
verification when they become pending. Only admins may update pending
services, other users only to an approved type (cf. `PendingService`);
their owners may delete them. Pending types stay pending if the mode
changes.

Users other than admins may only have added a limited number of types that
no service has (`custom.access_control.max_unused_service_types`; cf.
`check_unused_types()`), in every mode, so that they can't grow the list of
types (or of pending types) without bound.
"""

from itertools import islice
import json
import logging
from typing import Any

from flask import Response
from foca.security.access_control import get_current_subject, is_admin, resource_filter
from foca.utils.logging import escape_log_value
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import (
    CodedConflict,
    Forbidden,
    NotFound,
    problem,
)
from cloud_registry.ga4gh.registry.permissions import (
    current_user,
    may_add_service_types,
    service_types_pending,
    services_path,
)
from cloud_registry.ga4gh.registry.service import (
    PENDING_SERVICE,
    PROJECTION,
    VERIFIED,
    InvalidService,
    check_strings,
)
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)
# Fields of service types, in the order they are sorted by
TYPE_FIELDS = ("group", "artifact", "version")
# Projection of the fields of stored service types that are served
TYPE_PROJECTION = {"_id": False} | dict.fromkeys(TYPE_FIELDS, True)
# Internal field of stored service types: the user who added the type, or
# `None` for types added on startup (cf. `add_service_types()`); not served
CREATED_BY = "created_by"
# Internal field of stored service types: `True` while the type is pending,
# i.e., until an admin approves it (mode `moderated`); not set for approved
# types
PENDING = "pending"
# Query for approved service types
APPROVED = {PENDING: {"$ne": True}}
# Detail of conflicts when removing types that services have
IN_USE = (
    "Service type is in use. If you really want to revoke it, edit or delete "
    "all services that depend on it."
)
# Response of `POST /services/types`: the added type and status `201`, or
# an error
AddedResponse = tuple[dict[str, str], int] | Response
# Response of `DELETE /services/types`: the removed type, or an error
# (`group`, `artifact` and `version` are query parameters)
RemovedResponse = dict[str, str] | Response


class PendingService(CodedConflict):
    """A user other than an admin updates a pending service."""

    problem_code = "pending-service"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "The service has a service type that is pending until an admin "
            "approves it; until then, it can only be updated to an approved "
            "type, or deleted."
        )


# Number of types per query when counting the unused types of a user
UNUSED_TYPES_BATCH = 100


class UnusedTypesLimit(CodedConflict):
    """A user other than an admin has added too many unused types."""

    problem_code = "unused-types-limit"

    def __init__(self, limit: int) -> None:
        """Create the exception, with a description of what to do.

        Args:
            limit: How many unused types a user may have added.
        """
        super().__init__(
            f"You have added {limit} service types that no service has, as "
            "many as allowed; use them for services, or ask an admin to "
            "remove them, before adding others."
        )


class ServiceTypeRegistered(CodedConflict):
    """The service type to add is registered (also if it is pending)."""

    problem_code = "service-type-registered"

    def __init__(self) -> None:
        """Create the exception."""
        super().__init__("The service type is registered.")


class ServiceTypeInUse(CodedConflict):
    """The service type to remove is the type of services."""

    problem_code = "service-type-in-use"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(IN_USE)


class UnregisteredServiceType(InvalidService):
    """A service of a user who may not add types has an unregistered type."""

    def __init__(self, service_type: dict[str, str]) -> None:
        """Create the exception for a type that is not registered.

        Args:
            service_type: The service type.
        """
        super().__init__(
            f"The service type {describe_type(service_type)} is not registered: "
            "use one of the registered types (`GET /services/types`), or ask "
            "an admin to add it."
        )


def describe_type(service_type: dict[str, str]) -> str:
    """Describe a service type for messages.

    Args:
        service_type: Service type.

    Returns:
        The type as a JSON object, e.g., `{"group": "org.ga4gh", ...}`.
    """
    return json.dumps(service_type, ensure_ascii=False)


def type_document(value: object) -> dict[str, str] | None:
    """Get the stored form of a service type.

    Args:
        value: Type of a service (`type`).

    Returns:
        The type's `group`, `artifact` and `version`, or `None` if it isn't
        an object with these three strings (e.g., of services stored before
        their types were checked); further fields are ignored.
    """
    if not isinstance(value, dict):
        return None
    fields = {field: value.get(field) for field in TYPE_FIELDS}
    if not all(isinstance(item, str) for item in fields.values()):
        return None
    return {field: str(item) for field, item in fields.items()}


def service_type_exists(service_type: dict[str, str]) -> bool:
    """Check whether a service type is stored.

    Args:
        service_type: Service type, in its stored form (cf.
            `type_document()`).

    Returns:
        Whether the type is stored.
    """
    collection = get_collection("service_types")
    return collection.count_documents(dict(service_type), limit=1) > 0


def _type_in_use(service_type: dict[str, str]) -> bool:
    """Check whether a registered service has a type.

    Args:
        service_type: Service type, in its stored form.

    Returns:
        Whether a service has the type (`group`, `artifact` and `version` as
        written).
    """
    query = _services_of_type(service_type)
    return get_collection("services").count_documents(query, limit=1) > 0


def _services_of_type(service_type: dict[str, str]) -> dict[str, str]:
    """Get the query for the services that have a type.

    Args:
        service_type: Service type, in its stored form.

    Returns:
        Query for services with the type's `group`, `artifact` and `version`
        (as written).
    """
    return {f"type.{field}": service_type[field] for field in TYPE_FIELDS}


def is_approved(service_type: dict[str, str] | None) -> bool:
    """Check whether a service type is stored and approved.

    Args:
        service_type: Service type, in its stored form (cf.
            `type_document()`); `None` for invalid types.

    Returns:
        Whether the type is stored and not pending (cf. `PENDING`).
    """
    if service_type is None:
        return False
    collection = get_collection("service_types")
    return collection.count_documents({**service_type, **APPROVED}, limit=1) > 0


def store_pending_type(value: object) -> None:
    """Store the type of a service as pending, if the user's types are.

    Called after the service is validated (cf. `RegisterService.validate()`)
    and before it is stored, so that a service with a new type is never
    served as approved, not even briefly (or if storing its type fails). A
    type stays pending only if the registration fails concurrently (e.g., a
    duplicate registered meanwhile); admins remove it. Must be called in a
    request context.

    Args:
        value: Type of the service (`type`); not stored if it is invalid
            (cf. `type_document()`), as the registration rejects it.

    Raises:
        cloud_registry.ga4gh.registry.service.InvalidService: A string of the
            type can't be stored (cf. `check_strings()`).
    """
    service_type = type_document(value)
    if service_type is None or not service_types_pending():
        return
    check_strings(service_type)
    store_service_type(service_type, created_by=get_current_subject(), pending=True)


def _restore_type(service_type: dict[str, str], removed: dict[str, Any]) -> None:
    """Store a removed service type again, as it was.

    Also if it was stored meanwhile (e.g., as pending, by a registration).

    Args:
        service_type: Service type, in its stored form.
        removed: The removed document.
    """
    fields = {CREATED_BY: removed.get(CREATED_BY)}
    pending = removed.get(PENDING) is True
    update: dict[str, Any] = (
        {"$set": {**fields, PENDING: True}}
        if pending
        else {"$set": fields, "$unset": {PENDING: ""}}
    )
    result = get_collection("service_types").update_one(dict(service_type), update)
    if not result.matched_count:
        store_service_type(service_type, created_by=fields[CREATED_BY], pending=pending)
    # services registered meanwhile were flagged by the type then stored
    _sync_flags(service_type, _services_of_type(service_type))


def is_pending(service_type: dict[str, str] | None) -> bool:
    """Check whether a service type is pending.

    Args:
        service_type: Service type, in its stored form (cf.
            `type_document()`); `None` for invalid types.

    Returns:
        Whether the type is stored as pending (cf. `PENDING`).
    """
    if service_type is None:
        return False
    collection = get_collection("service_types")
    return collection.count_documents({**service_type, PENDING: True}, limit=1) > 0


def _sync_flags(service_type: dict[str, str], query: dict[str, Any]) -> None:
    """Set or clear the pending flags of services by their type.

    Writes first, then checks the type again, and writes again if its state
    changed meanwhile (e.g., approved, or removed and added again as
    pending), so that concurrent calls, approvals and removals of the type
    end with the flags of its last state: each change of the state is
    followed by a call (cf. `putServiceTypeApproval()`, `_restore_type()`),
    and either that call or the check here sees the other's write.

    Args:
        service_type: Type of the services, in its stored form.
        query: Query for the services (of that type, as written).
    """
    services = get_collection("services")
    pending = is_pending(service_type)
    while True:
        if pending:
            services.update_many(
                # pending services aren't verified (also those flagged before)
                {
                    **query,
                    "$or": [
                        {PENDING_SERVICE: {"$ne": True}},
                        {VERIFIED: {"$exists": True}},
                    ],
                },
                {"$set": {PENDING_SERVICE: True}, "$unset": {VERIFIED: ""}},
            )
        else:
            services.update_many(
                {**query, PENDING_SERVICE: True}, {"$unset": {PENDING_SERVICE: ""}}
            )
        changed = is_pending(service_type)
        if changed == pending:
            return
        pending = changed


def sync_pending_flag(serviceId: str, service_type: dict[str, str]) -> None:
    """Set or clear the pending flag of a stored service by its type.

    Called after a service is stored, with the flag as read before (cf.
    `RegisterService.pending`), in case its type was approved (or removed and
    added again) meanwhile. Only changes the service if it still has the
    type, so that a concurrent change of its type isn't overwritten.

    Args:
        serviceId: Service identifier.
        service_type: Type of the service, in its stored form (cf.
            `type_document()`).
    """
    _sync_flags(service_type, {"id": serviceId, **_services_of_type(service_type)})


def sync_pending_services() -> None:
    """Set the pending flags of the stored services by their types.

    Run on startup, so that flags missed by interrupted requests are
    corrected; idempotent, and safe to run concurrently (cf.
    `_sync_flags()`). Only checks the pending types, and the types of the
    services flagged as pending, not all types.
    """
    pending = list(
        get_collection("service_types").find({PENDING: True}, TYPE_PROJECTION)
    )
    flagged = get_collection("services").aggregate(
        [
            {"$match": {PENDING_SERVICE: True}},
            {"$group": {"_id": {field: f"$type.{field}" for field in TYPE_FIELDS}}},
        ]
    )
    for value in [*pending, *(group["_id"] for group in flagged)]:
        service_type = type_document(value)
        if service_type is not None:
            _sync_flags(service_type, _services_of_type(service_type))


def visible_services() -> dict[str, Any]:
    """Get the filter for the services that the current user may see.

    Must be called in a request context. The filter has the same size for
    any number of pending types or services.

    Returns:
        Filter for all services if no service is pending (one indexed
        lookup) or the user is an admin; otherwise, for the services that
        aren't pending (cf. `PENDING_SERVICE`), and those the user has a role
        on (also through groups; cf. FOCA's `resource_filter()`).
    """
    if (
        get_collection("services").find_one({PENDING_SERVICE: True}, {"_id": True})
        is None
    ):
        return {}
    own = resource_filter(services_path())
    if not own:
        # admins see all services
        return {}
    return {"$or": [{PENDING_SERVICE: {"$ne": True}}, own]}


def _count_unused_types(user: str, limit: int) -> int:
    """Count the types that a user added and no service has, up to a limit.

    Approved types count too, as approval doesn't give them services.
    Takes one indexed query per `UNUSED_TYPES_BATCH` types of the user, up
    to the batch with the `limit`-th unused one, as users may have added any
    number of types that services have (a `$lookup` would take one query,
    but mongomock, which the tests also run with, lacks it).

    Args:
        user: User identifier (cf. `CREATED_BY`).
        limit: Number at which to stop counting.

    Returns:
        Number of such types, at most `limit`.
    """
    count = 0
    types = get_collection("service_types").find({CREATED_BY: user}, TYPE_PROJECTION)
    while batch := list(islice(types, UNUSED_TYPES_BATCH)):
        used = get_collection("services").aggregate(
            [
                {"$match": {"$or": [_services_of_type(item) for item in batch]}},
                {"$group": {"_id": {field: f"$type.{field}" for field in TYPE_FIELDS}}},
            ]
        )
        in_use = {_type_key(group["_id"]) for group in used}
        count += sum(_type_key(item) not in in_use for item in batch)
        if count >= limit:
            return limit
    return count


def _type_key(service_type: dict[str, Any]) -> tuple[Any, ...]:
    """Get a hashable key of a service type.

    Args:
        service_type: Service type (`group`, `artifact` and `version`).

    Returns:
        The values of the type's fields, in order.
    """
    return tuple(service_type.get(field) for field in TYPE_FIELDS)


def check_unused_types() -> None:
    """Check that the current user may add another type that no service has.

    Must be called in a request context, before a type is added. Concurrent
    requests of a user may exceed the limit by their number.

    Raises:
        UnusedTypesLimit: The user is not an admin, and has added as many
            types that no service has as allowed
            (`custom.access_control.max_unused_service_types`).
    """
    limit = get_custom_config().access_control.max_unused_service_types
    subject = get_current_subject()
    if limit is None or subject is None or is_admin():
        return
    if _count_unused_types(subject, limit) >= limit:
        logger.info(
            "Service type not added: '%s' added %d types that no service has.",
            current_user(),
            limit,
        )
        raise UnusedTypesLimit(limit)


def visible_types() -> dict[str, Any]:
    """Get the filter for the service types that the current user may see.

    Must be called in a request context.

    Returns:
        Filter for all types if the user is an admin; otherwise, for the
        approved types, and the pending types the user added.
    """
    if is_admin():
        return {}
    subject = get_current_subject()
    if subject is None:
        return APPROVED
    return {"$or": [APPROVED, {CREATED_BY: subject}]}


def store_service_type(
    service_type: dict[str, str], created_by: str | None = None, pending: bool = False
) -> bool:
    """Store a service type, unless it is stored.

    Concurrent calls for the same type are safe.

    Args:
        service_type: Service type, in its stored form (cf.
            `type_document()`).
        created_by: User who adds the type (cf. `CREATED_BY`); kept if the
            type is stored.
        pending: Whether the type is pending (cf. `PENDING`); a stored type
            stays as it is.

    Returns:
        Whether the type was added.
    """
    fields: dict[str, Any] = {CREATED_BY: created_by}
    if pending:
        fields[PENDING] = True
    try:
        result = get_collection("service_types").update_one(
            filter=dict(service_type),
            update={"$setOnInsert": fields},
            upsert=True,
        )
    except DuplicateKeyError:
        # added concurrently
        return False
    if result.upserted_id is None:
        return False
    logger.info("Added service type %s.", escape_log_value(describe_type(service_type)))
    return True


def check_service_type(value: object) -> None:
    """Check that the current user may register a service with a type.

    Must be called in a request context.

    Args:
        value: Type of the service (`type`); not checked if it is invalid
            (cf. `type_document()`), as the registration rejects it.

    Raises:
        cloud_registry.ga4gh.registry.service.InvalidService: A string of the
            type can't be stored (cf. `check_strings()`).
        UnregisteredServiceType: The user may not add types (cf.
            `may_add_service_types()`), and the type is not stored.
        UnusedTypesLimit: The type is not stored, and the user has added as
            many types that no service has as allowed (cf.
            `check_unused_types()`): registering services with new types and
            changing them leaves types unused, too.
    """
    service_type = type_document(value)
    if service_type is None:
        return
    check_strings(service_type, field="type")
    if service_type_exists(service_type):
        return
    if not may_add_service_types():
        raise UnregisteredServiceType(service_type)
    check_unused_types()


def add_service_types() -> None:
    """Store the types of registered services that are not stored.

    For example, of services registered before types were stored. Run on
    startup; idempotent, and safe to run concurrently (e.g., in each worker
    of the app). Types that are not objects with the strings `group`,
    `artifact` and `version` (cf. `type_document()`) are skipped.
    """
    types = set()
    for service in get_collection("services").find({}, {"_id": False, "type": True}):
        service_type = type_document(service.get("type"))
        if service_type is not None:
            types.add(tuple(service_type.items()))
    added = sum(store_service_type(dict(service_type)) for service_type in types)
    if added:
        logger.info("Added %d service types of registered services.", added)


def list_service_types(query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List the stored service types.

    Args:
        query: Query for the types to list (e.g., cf. `visible_types()`);
            all if `None`.

    Returns:
        Service types, sorted by group, artifact and version.
    """
    records = get_collection("service_types").find(
        filter={} if query is None else query,
        projection=TYPE_PROJECTION,
        sort=[(field, 1) for field in TYPE_FIELDS],
    )
    return list(records)


def _requested_type(service_type: dict[str, Any]) -> dict[str, str]:
    """Get the service type in a request body, in its stored form.

    Args:
        service_type: Service type (request body).

    Returns:
        The type (cf. `type_document()`).

    Raises:
        cloud_registry.ga4gh.registry.service.InvalidService: A string can't
            be stored (cf. `check_strings()`), or the type doesn't have the
            three strings, which the request schema requires.
    """
    check_strings(service_type)
    document = type_document(service_type)
    if document is None:
        raise InvalidService(
            "Service types need the strings 'group', 'artifact' and 'version'."
        )
    return document


# POST /services/types
def postServiceType(service_type: dict[str, Any], **kwargs: Any) -> AddedResponse:
    """Add a service type.

    Args:
        service_type: Service type (request body).
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The added type, with status `201`; pending if the user's types are
        (cf. `service_types_pending()`). Or an error response: `400` for
        strings that can't be stored, `403` if the user may not add types
        (cf. `may_add_service_types()`), `409` if the type is registered
        (also if it is pending) or the user has added as many unused types
        as allowed (cf. `check_unused_types()`).
    """
    if not may_add_service_types():
        logger.info(
            "Service type not added: '%s' may not add service types.", current_user()
        )
        return problem(Forbidden("Only admins may add service types."))
    try:
        document = _requested_type(service_type)
    except InvalidService as exc:
        return problem(exc)
    if service_type_exists(document):
        return problem(ServiceTypeRegistered())
    try:
        check_unused_types()
    except UnusedTypesLimit as exc:
        return problem(exc)
    try:
        # insert a copy, as `insert_one()` adds an `_id` field
        pending = service_types_pending()
        get_collection("service_types").insert_one(
            {**document, CREATED_BY: get_current_subject()}
            | ({PENDING: True} if pending else {})
        )
    except DuplicateKeyError:
        return problem(ServiceTypeRegistered())
    logger.info(
        "Service type %s added by '%s'%s.",
        escape_log_value(describe_type(document)),
        current_user(),
        " (pending)" if pending else "",
    )
    return document, 201


# DELETE /services/types
def deleteServiceType(
    group: str, artifact: str, version: str, **kwargs: Any
) -> RemovedResponse:
    """Remove a service type that no registered service has.

    The type is named by query parameters, as request bodies of `DELETE`
    have no defined meaning (RFC 9110), and Connexion doesn't pass them.
    If a service with the type is registered concurrently, after the type was
    removed, the type is stored again, and the request fails.

    Args:
        group: Group of the type.
        artifact: Artifact of the type.
        version: Version of the type.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The removed type. Or an error response: `409` if a registered
        service has the type (also if the type isn't stored, e.g., of a
        service registered by an older version of the app during an
        upgrade).

    Raises:
        cloud_registry.exceptions.NotFound: The type is not stored.
    """
    document = {"group": group, "artifact": artifact, "version": version}
    if _type_in_use(document):
        return problem(ServiceTypeInUse())
    removed = get_collection("service_types").find_one_and_delete(dict(document))
    if removed is None:
        raise NotFound
    if _type_in_use(document):
        # a service with the type was registered meanwhile
        _restore_type(document, removed)
        return problem(ServiceTypeInUse())
    logger.info(
        "Service type %s removed by '%s'.",
        escape_log_value(describe_type(document)),
        current_user(),
    )
    return document


# PUT /services/types/approval
def putServiceTypeApproval(
    service_type: dict[str, Any], **kwargs: Any
) -> dict[str, str] | Response:
    """Approve a pending service type, and so the services that have it.

    Only for admins: no policy in `config.yaml` allows it to other users.

    Args:
        service_type: Service type (request body).
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The approved type, also if it was approved before. Or an error
        response: `400` for strings that can't be stored.

    Raises:
        cloud_registry.exceptions.NotFound: The type is not stored.
    """
    try:
        document = _requested_type(service_type)
    except InvalidService as exc:
        return problem(exc)
    approved = get_collection("service_types").find_one_and_update(
        {**document, PENDING: True}, {"$unset": {PENDING: ""}}
    )
    # also if the type was approved before, to clear flags missed meanwhile
    _sync_flags(document, _services_of_type(document))
    if approved is None:
        if not service_type_exists(document):
            raise NotFound
        return document
    logger.info(
        "Service type %s approved by '%s'.",
        escape_log_value(describe_type(document)),
        current_user(),
    )
    return document


# GET /approvals
def getApprovals(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
    """List the pending service types and services the current user may see.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Pending service types (`service_types`), sorted by group, artifact
        and version: all for admins, those the user added for other users;
        and pending services (`services`), sorted by identifier: all for
        admins, those the user has a role on for other users.
    """
    types = list_service_types({"$and": [{PENDING: True}, visible_types()]})
    records = get_collection("services").find(
        {PENDING_SERVICE: True, **resource_filter(services_path())},
        PROJECTION,
        sort=[("id", 1)],
    )
    return {"service_types": types, "services": list(records)}
