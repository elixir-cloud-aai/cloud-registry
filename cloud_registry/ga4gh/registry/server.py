"""Controllers for service endpoints."""

from json import dumps
import logging

from flask import Response, request
from foca.utils.logging import log_traffic

from cloud_registry.exceptions import BadRequest, NotFound, exceptions
from cloud_registry.ga4gh.registry.permissions import add_owner, remove_permissions
from cloud_registry.ga4gh.registry.service import (
    PROJECTION,
    DuplicateService,
    InvalidService,
    RegisterService,
)
from cloud_registry.ga4gh.registry.service_info import get_service_info
from cloud_registry.utils import get_collection, get_public_url

logger = logging.getLogger(__name__)
# Response of a created service: identifier, status and headers
Created = tuple[str, int, dict[str, str]]


# GET /services
@log_traffic
def getServices(**kwargs) -> list:
    """List all services.

    Returns:
        List of services.
    """
    db_collection_service = get_collection("services")
    records = db_collection_service.find(
        filter={},
        projection=PROJECTION,
    )
    return list(records)


# GET /services/{serviceId}
@log_traffic
def getServiceById(serviceId: str, **kwargs) -> dict:
    """Retrieve service by its identifier.

    Args:
        serviceId: Identifier of service to be retrieved.

    Returns:
        Service object.
    """
    db_collection_service = get_collection("services")
    obj = db_collection_service.find_one({"id": serviceId}, PROJECTION)
    if not obj:
        raise NotFound
    return obj


# GET /services/types
@log_traffic
def getServiceTypes(**kwargs) -> list:
    """List types of services.

    Returns:
        List of distinct service types, sorted by group, artifact and version.
    """
    fields = ("group", "artifact", "version")
    records = get_collection("services").aggregate(
        [
            {"$group": {"_id": {field: f"$type.{field}" for field in fields}}},
            {"$sort": {f"_id.{field}": 1 for field in fields}},
        ]
    )
    return [record["_id"] for record in records]


# GET /service-info
@log_traffic
def getServiceInfo(**kwargs) -> dict:
    """Show information about this service.

    Returns:
        Service info object.
    """
    return get_service_info()


# POST /services
@log_traffic
def postService(**kwargs) -> str | Created | Response:
    """Add service with an auto-generated identifier.

    Returns:
        Identifier of registered service, with status `201` and its URL as
        `Location`.
    """
    return _register_service()


# DELETE /services/{serviceId}
@log_traffic
def deleteService(serviceId: str, **kwargs) -> str:
    """Delete service.

    Args:
        serviceId: Identifier of service to be deleted.

    Returns:
        Identifier of deleted service.
    """
    db_collection_service = get_collection("services")
    res = db_collection_service.delete_one({"id": serviceId})
    if not res.deleted_count:
        raise NotFound
    remove_permissions(serviceId)
    return serviceId


# PUT /services/{serviceId}
@log_traffic
def putService(serviceId: str, **kwargs) -> str | Created | Response:
    """Add/replace service with a user-supplied ID.

    Args:
        serviceId: Identifier of service to be registered/updated.

    Returns:
        Identifier of registered/updated service; for registered services,
        with status `201` and their URL as `Location`.
    """
    return _register_service(service_id=serviceId)


def _get_request_object() -> dict:
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


def _register_service(service_id: str | None = None) -> str | Created | Response:
    """Register the service in the request body.

    Args:
        service_id: Identifier of the service; generated if not provided.

    Returns:
        Identifier of the service; if it is new, with status `201` and its URL
        as `Location`. Or an error response: `400` for invalid URLs, `409`
        naming the registered service with the same URL and API type.
    """
    service = RegisterService(data=_get_request_object(), id=service_id)
    try:
        service.register_metadata()
    except InvalidService as exc:
        logger.info("Service not registered: %s", exc.description)
        return _problem(exc)
    except DuplicateService as exc:
        logger.info("Service not registered: %s", exc.description)
        return _problem(exc, serviceId=exc.existing_id)
    if service.was_created:
        try:
            # Roles left on the identifier by a deleted service (e.g., if
            # revoking them failed) don't apply to the new one
            remove_permissions(service.data["id"])
            add_owner(service.data["id"])
        except ValueError as exc:
            # FOCA rejects values that can't be stored in policy rules (e.g.,
            # service identifiers with commas)
            logger.info("Can't store owner role: %s", exc)
            _unregister_service(service)
            raise BadRequest from exc
        except Exception:
            _unregister_service(service)
            raise
        # PUT requests are made to the URL of the service, POST requests to
        # that of the collection
        path = request.path
        if service_id is None:
            path = f"{path}/{service.data['id']}"
        return service.data["id"], 201, {"Location": get_public_url(path)}
    return service.data["id"]


def _problem(exc: InvalidService | DuplicateService, **extensions: str) -> Response:
    """Build a problem details response with the description of an exception.

    FOCA's problem details are fixed per exception class, so responses with
    details specific to a request are built here.

    Args:
        exc: Exception with a request-specific description.
        extensions: Extension members of the problem details.

    Returns:
        Problem details response.
    """
    # title as for other errors, from the closest mapped exception class
    title = next(
        exceptions[cls]["title"] for cls in type(exc).__mro__ if cls in exceptions
    )
    problem = {
        "title": title,
        "status": exc.code,
        "detail": exc.description,
        **extensions,
    }
    return Response(
        dumps(problem), status=exc.code, mimetype="application/problem+json"
    )


def _unregister_service(service: RegisterService) -> None:
    """Delete a new service whose owner couldn't be stored, as only admins
    could manage it, with any roles stored for it.

    Args:
        service: Registered service.
    """
    service.db_coll.delete_one({"id": service.data["id"]})
    remove_permissions(service.data["id"])
