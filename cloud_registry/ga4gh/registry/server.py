"""Controllers for service endpoints."""

import logging

from flask import Response, request
from foca.utils.logging import log_traffic

from cloud_registry.exceptions import BadRequest, NotFound
from cloud_registry.ga4gh.registry.permissions import add_owner, remove_permissions
from cloud_registry.ga4gh.registry.service import RegisterService
from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)


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
        projection={"_id": False},
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
    obj = db_collection_service.find_one({"id": serviceId}, {"_id": False})
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
    service_info = RegisterServiceInfo()
    return service_info.get_service_info()


# POST /services
@log_traffic
def postService(**kwargs) -> str:
    """Add service with an auto-generated identifier.

    Returns:
        Identifier of registered service.
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
def putService(serviceId: str, **kwargs) -> str:
    """Add/replace service with a user-supplied ID.

    Args:
        serviceId: Identifier of service to be registered/updated.

    Returns:
        Identifier of registered/updated service.
    """
    return _register_service(service_id=serviceId)


# POST /service-info
@log_traffic
def postServiceInfo(**kwargs) -> Response:
    """Set information about this service.

    Returns:
        An empty 201 response with a `Location` header.
    """
    service_info = RegisterServiceInfo()
    headers = service_info.set_service_info_from_app_context(data=_get_request_object())
    response = Response(status=201, headers=headers)
    # The body is empty, so it has no media type (Flask sets a default one)
    del response.headers["Content-Type"]
    return response


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


def _register_service(service_id: str | None = None) -> str:
    """Register the service in the request body.

    Args:
        service_id: Identifier of the service; generated if not provided.

    Returns:
        Identifier of registered service.
    """
    service = RegisterService(data=_get_request_object(), id=service_id)
    service.register_metadata()
    if service.was_created:
        try:
            # Roles left on the identifier by a deleted service (e.g., if
            # revoking them failed) don't apply to the new one
            remove_permissions(service.data["id"])
            add_owner(service.data["id"])
        except Exception as exc:
            # Without an owner, only admins could manage the new service
            service.db_coll.delete_one({"id": service.data["id"]})
            if isinstance(exc, ValueError):
                # FOCA rejects values that can't be stored in policy rules
                # (e.g., identifiers with commas)
                logger.info("Invalid service identifier: %s", exc)
                raise BadRequest from exc
            raise
    return service.data["id"]
