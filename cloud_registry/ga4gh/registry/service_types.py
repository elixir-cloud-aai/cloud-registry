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
after the service (cf. `store_service_type()`), types are only removed if no
service has them, also when a service is registered concurrently, and the
types of services registered before types were stored are added on startup
(cf. `add_service_types()`).
"""

import json
import logging
from typing import Any

from flask import Response
from foca.security.access_control import get_current_subject
from foca.utils.logging import escape_log_value
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import (
    Conflict,
    Forbidden,
    NotFound,
    problem,
)
from cloud_registry.ga4gh.registry.permissions import (
    current_user,
    may_add_service_types,
)
from cloud_registry.ga4gh.registry.service import InvalidService, check_strings
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)
# Fields of service types, in the order they are sorted by
TYPE_FIELDS = ("group", "artifact", "version")
# Projection of the fields of stored service types that are served
TYPE_PROJECTION = {"_id": False} | dict.fromkeys(TYPE_FIELDS, True)
# Internal field of stored service types: the user who added the type, or
# `None` for types added on startup (cf. `add_service_types()`); not served
CREATED_BY = "created_by"
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
    query = {f"type.{field}": value for field, value in service_type.items()}
    return get_collection("services").count_documents(query, limit=1) > 0


def store_service_type(
    service_type: dict[str, str], created_by: str | None = None
) -> bool:
    """Store a service type, unless it is stored.

    Concurrent calls for the same type are safe.

    Args:
        service_type: Service type, in its stored form (cf.
            `type_document()`).
        created_by: User who adds the type (cf. `CREATED_BY`); kept if the
            type is stored.

    Returns:
        Whether the type was added.
    """
    try:
        result = get_collection("service_types").update_one(
            filter=dict(service_type),
            update={"$setOnInsert": {CREATED_BY: created_by}},
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
        UnregisteredServiceType: The user may not add types (cf.
            `may_add_service_types()`), and the type is not stored.
    """
    service_type = type_document(value)
    if service_type is None or may_add_service_types():
        return
    if not service_type_exists(service_type):
        raise UnregisteredServiceType(service_type)


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


def list_service_types() -> list[dict[str, Any]]:
    """List the stored service types.

    Returns:
        Service types, sorted by group, artifact and version.
    """
    records = get_collection("service_types").find(
        filter={},
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
        The added type, with status `201`. Or an error response: `400` for
        strings that can't be stored, `403` if the user may not add types
        (cf. `may_add_service_types()`), `409` if the type is registered.
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
    try:
        # insert a copy, as `insert_one()` adds an `_id` field
        get_collection("service_types").insert_one(
            {**document, CREATED_BY: get_current_subject()}
        )
    except DuplicateKeyError:
        return problem(Conflict("The service type is registered."))
    logger.info(
        "Service type %s added by '%s'.",
        escape_log_value(describe_type(document)),
        current_user(),
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
        service has the type.

    Raises:
        cloud_registry.exceptions.NotFound: The type is not stored.
    """
    document = {"group": group, "artifact": artifact, "version": version}
    in_use = Conflict(IN_USE)
    if not service_type_exists(document):
        raise NotFound
    if _type_in_use(document):
        return problem(in_use)
    removed = get_collection("service_types").find_one_and_delete(dict(document))
    if removed is None:
        raise NotFound
    if _type_in_use(document):
        # a service with the type was registered meanwhile
        store_service_type(document, created_by=removed.get(CREATED_BY))
        return problem(in_use)
    logger.info(
        "Service type %s removed by '%s'.",
        escape_log_value(describe_type(document)),
        current_user(),
    )
    return document
