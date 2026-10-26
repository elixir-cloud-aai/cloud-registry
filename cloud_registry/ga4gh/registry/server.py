"""Controllers for service endpoints."""

import logging
from typing import Any

from flask import Response, request
from foca.security.access_control import get_current_subject, is_admin
from foca.utils.logging import escape_log_value, log_traffic

from cloud_registry.exceptions import (
    BadRequest,
    NotFound,
    problem,
    shorten_detail,
)
from cloud_registry.ga4gh.registry.permissions import (
    add_owner,
    check_read_token,
    current_user,
    may_add_service_types,
    may_read_service,
    remove_owner,
    remove_permissions,
    service_types_pending,
)
from cloud_registry.ga4gh.registry.service import (
    PENDING_SERVICE,
    PROJECTION,
    DuplicateService,
    InvalidService,
    RegisterService,
    check_strings,
    service_exists,
)
from cloud_registry.ga4gh.registry.service_types import (
    PendingService,
    UnusedTypesLimit,
    check_service_type,
    is_approved,
    is_pending,
    list_service_types,
    store_pending_type,
    store_service_type,
    sync_pending_flag,
    type_document,
    visible_services,
    visible_types,
)
from cloud_registry.utils import get_collection, get_public_url

logger = logging.getLogger(__name__)
# Response for a registered service: its identifier, status `201` and its URL
# as `Location` header
CreatedResponse = tuple[str, int, dict[str, str]]
# Response of a registration: the identifier of a replaced service, a
# registered service, or an error
RegisterResponse = str | CreatedResponse | Response


# GET /services
@log_traffic
@check_read_token
def getServices(**kwargs: Any) -> list[dict[str, Any]]:
    """List all services.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        List of services, without the pending services that the user may
        not see (cf. `visible_services()`).
    """
    db_collection_service = get_collection("services")
    records = db_collection_service.find(
        filter=visible_services(),
        projection=PROJECTION,
    )
    return list(records)


# GET /services/{serviceId}
@log_traffic
@check_read_token
def getServiceById(serviceId: str, **kwargs: Any) -> dict[str, Any]:
    """Retrieve service by its identifier.

    Args:
        serviceId: Identifier of service to be retrieved.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Service object.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, or is
            pending and the user may not see it (cf. `visible_services()`).
    """
    db_collection_service = get_collection("services")
    obj = db_collection_service.find_one(
        {"id": serviceId, **visible_services()}, PROJECTION
    )
    if not obj:
        raise NotFound
    return obj


# GET /services/types
@log_traffic
@check_read_token
def getServiceTypes(**kwargs: Any) -> list[dict[str, Any]]:
    """List types of services.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        List of the stored service types (cf.
        `cloud_registry.ga4gh.registry.service_types`), sorted by group,
        artifact and version; without the pending types that the user may
        not see (cf. `visible_types()`).
    """
    return list_service_types(visible_types())


# POST /services
@log_traffic
def postService(**kwargs: Any) -> RegisterResponse:
    """Add service with an auto-generated identifier.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`, and the request body); unused.

    Returns:
        Identifier of registered service, with status `201` and its URL as
        `Location`.
    """
    return _register_service(collection_path=request.path)


# DELETE /services/{serviceId}
@log_traffic
def deleteService(serviceId: str, **kwargs: Any) -> str:
    """Delete service.

    The roles on the service are revoked first, so that none are left on a
    deleted service if deleting it fails, and nobody can act on it in
    between; and again afterwards, as roles granted concurrently (after
    their request checked that the service exists) would be left otherwise.

    Args:
        serviceId: Identifier of service to be deleted.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier of deleted service.
    """
    db_collection_service = get_collection("services")
    if not service_exists(serviceId):
        raise NotFound
    remove_permissions(serviceId)
    res = db_collection_service.delete_one({"id": serviceId})
    if not res.deleted_count:
        raise NotFound
    # a no-op unless a role was granted during the deletion (logged only if
    # roles are revoked)
    remove_permissions(serviceId)
    logger.info(
        "Service '%s' deleted by '%s'.", escape_log_value(serviceId), current_user()
    )
    return serviceId


# PUT /services/{serviceId}
@log_traffic
def putService(serviceId: str, **kwargs: Any) -> RegisterResponse:
    """Add/replace service with a user-supplied ID.

    Only admins may add services: for others, the service must exist (cf.
    `RegisterService._replace()`), also if it is deleted concurrently.

    Args:
        serviceId: Identifier of service to be registered/updated.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`, and the request body); unused.

    Returns:
        Identifier of registered/updated service; for registered services,
        with status `201` and their URL as `Location`.
    """
    # the request path is the service's URL path: `/services/{serviceId}`,
    # with the API base path and the identifier as routed (decoded)
    collection_path = request.path.removesuffix(f"/{serviceId}")
    return _register_service(
        collection_path=collection_path, service_id=serviceId, allow_create=is_admin()
    )


def _get_request_object() -> dict[str, Any]:
    """Get the JSON object in the request body.

    Returns:
        Request body.

    Raises:
        cloud_registry.exceptions.BadRequest: The body is not a JSON object.
    """
    request_json = request.json
    if not isinstance(request_json, dict):
        logger.error("Invalid request payload.")
        raise BadRequest
    return request_json


def _register_service(
    collection_path: str, service_id: str | None = None, allow_create: bool = True
) -> RegisterResponse:
    """Register the service in the request body.

    The type of the service is stored after the service, unless it is
    stored (cf. `cloud_registry.ga4gh.registry.service_types`), or before
    it if it is pending (cf. `store_pending_type()`). Users who
    may not add types (cf. `may_add_service_types()`) can only register
    services with a stored type; the types that users add are pending if
    the deployment moderates them (cf. `service_types_pending()`). Only
    admins may update pending services, other users only to an approved
    type; checked before the update, so an update may still pass if its
    type becomes pending meanwhile (e.g., the type of the stored service is
    removed and added again).

    Args:
        collection_path: Path of the service collection (`/services`, with the
            API base path), for the URL of new services.
        service_id: Identifier of the service; generated if not provided.
        allow_create: With `service_id`, whether the service may be
            registered if it doesn't exist.

    Returns:
        Identifier of the service; if it is new, with status `201` and its URL
        as `Location`. Or an error response: `400` for invalid URLs or types
        the user may not add, `409` for a registered service with the same
        URL and API type, naming it only if the user may read it (cf.
        `may_read_service()` and `visible_services()`), or for updates of
        pending services by users other than admins.

    Raises:
        cloud_registry.exceptions.BadRequest: The owner role can't be stored
            for the service identifier.
        cloud_registry.exceptions.NotFound: A new service was deleted
            meanwhile.
    """
    # admins may set the timestamps (e.g., to keep those of imported
    # services); the registry sets them for everybody else
    service = RegisterService(
        data=_get_request_object(),
        id=service_id,
        allow_create=allow_create,
        keep_timestamps=is_admin(),
    )
    if service_id is not None and not is_admin():
        refused = _check_pending_update(service_id, service.data.get("type"))
        if refused is not None:
            return refused
    try:
        check_service_type(service.data.get("type"))
        # pending types are only stored for valid services
        service.validate()
        store_pending_type(service.data.get("type"))
        service.pending = is_pending(type_document(service.data.get("type")))
        service.register_metadata()
    except (InvalidService, UnusedTypesLimit) as exc:
        logger.info(
            "Service not registered: %s",
            escape_log_value(shorten_detail(exc.description)),
        )
        return problem(exc)
    except DuplicateService as exc:
        logger.info(
            "Service not registered: %s",
            escape_log_value(shorten_detail(exc.description)),
        )
        # only users who may read the registered service learn which one it
        # is, by the same decision as for `GET /services/{serviceId}`
        if may_read_service(exc.existing_id) and _may_see(exc.existing_id):
            return problem(exc, serviceId=exc.existing_id)
        # FOCA logs the denied check like a request
        logger.info(
            "Registered service with the same URL and API type not named: "
            "'%s' may not read it.",
            current_user(),
        )
        return problem(exc.hidden())
    # also if the type was removed concurrently, as a service has it now;
    # without the user if they may not add types (only then)
    service_type = type_document(service.data["type"])
    if service_type is not None:
        created_by = get_current_subject() if may_add_service_types() else None
        store_service_type(
            service_type, created_by=created_by, pending=service_types_pending()
        )
        # if the type was approved (or removed and added) meanwhile
        sync_pending_flag(service.data["id"], service_type)
    if service.verification_removed:
        logger.info(
            "Verification of service '%s' removed by an update by '%s' (URL or "
            "API type changed).",
            escape_log_value(service.data["id"]),
            current_user(),
        )
    if service.was_created:
        try:
            # Roles left on the identifier by a deleted service (e.g., if
            # revoking them failed) don't apply to the new one
            remove_permissions(service.data["id"])
            add_owner(service.data["id"])
        except ValueError as exc:
            # FOCA rejects values that can't be stored in policy rules (e.g.,
            # service identifiers with commas)
            logger.info("Can't store owner role: %s", escape_log_value(exc))
            _unregister_service(service)
            raise BadRequest from exc
        except Exception:
            _unregister_service(service)
            raise
        # The service may have been deleted (with its roles) before its owner
        # was stored, which would leave the role on a deleted service (cf.
        # `exists` of FOCA's `grant_role()`); only the role granted here is
        # revoked, as the identifier may be registered again meanwhile
        if not service_exists(service.data["id"]):
            remove_owner(service.data["id"])
            raise NotFound
        path = f"{collection_path.rstrip('/')}/{service.data['id']}"
        return service.data["id"], 201, {"Location": get_public_url(path)}
    return service.data["id"]


def _check_pending_update(serviceId: str, value: object) -> Response | None:
    """Check an update of a service by a user other than an admin.

    Args:
        serviceId: Service identifier.
        value: Type of the service in the update (`type`).

    Returns:
        `None` if the service isn't pending, or the update has an approved
        type, which ends the review (e.g., to undo a change of the type).
        Otherwise, an error response: `400` for strings of the type that
        can't be stored (nor queried), `409` for pending services.
    """
    if not _is_pending(serviceId):
        return None
    new_type = type_document(value)
    try:
        check_strings(new_type or {}, field="type")
    except InvalidService as exc:
        return problem(exc)
    if is_approved(new_type):
        return None
    pending = PendingService()
    logger.info(
        "Service '%s' not updated: %s",
        escape_log_value(serviceId),
        escape_log_value(pending.description),
    )
    return problem(pending)


def _is_pending(serviceId: str) -> bool:
    """Check whether a stored service is pending.

    Args:
        serviceId: Service identifier.

    Returns:
        Whether the service exists and is pending (cf. `PENDING_SERVICE`).
    """
    query = {"id": serviceId, PENDING_SERVICE: True}
    return get_collection("services").count_documents(query, limit=1) > 0


def _may_see(serviceId: str) -> bool:
    """Check whether a service exists and is not hidden from the current user.

    Args:
        serviceId: Service identifier.

    Returns:
        Whether the service is not pending, or the user may see it (cf.
        `visible_services()`); `False` if it doesn't exist.
    """
    query = {"id": serviceId, **visible_services()}
    return get_collection("services").count_documents(query, limit=1) > 0


def _unregister_service(service: RegisterService) -> None:
    """Delete a new service whose owner couldn't be stored.

    Only admins could manage it otherwise. Roles stored for it are revoked.

    Args:
        service: Registered service.
    """
    service.db_coll.delete_one({"id": service.data["id"]})
    remove_permissions(service.data["id"])
