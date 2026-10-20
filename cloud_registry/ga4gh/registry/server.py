"""Controllers for service endpoints."""

import logging

from flask import request
from foca.utils.logging import log_traffic

from cloud_registry.exceptions import BadRequest, NotFound
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
    obj = db_collection_service.find_one({"id": serviceId})
    if not obj:
        raise NotFound
    del obj["_id"]
    return obj


# GET /services/types
@log_traffic
def getServiceTypes(**kwargs) -> list:
    """List types of services.

    Returns:
        List of distinct service types.
    """
    services = getServices.__wrapped__()  # type: ignore[attr-defined]
    types = [s["type"] for s in services]
    uniq_types = [dict(t) for t in {tuple(sorted(d.items())) for d in types}]

    return uniq_types


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
    request_json = request.json
    if isinstance(request_json, dict):
        service = RegisterService(data=request_json)
        service.register_metadata()
        return service.data["id"]
    else:
        logger.error("Invalid request payload.")
        raise BadRequest


# DELETE /services/{serviceId}
@log_traffic
def deleteService(serviceId: str, **kwargs) -> str:
    """Delete service.

    Args:
        id: Identifier of service to be deleted.

    Returns:
        Identifier of deleted service.
    """
    db_collection_service = get_collection("services")
    res = db_collection_service.delete_one({"id": serviceId})
    if not res.deleted_count:
        raise NotFound
    return serviceId


# PUT /services/{serviceId}
@log_traffic
def putService(serviceId: str, **kwargs) -> str:
    """Add/replace service with a user-supplied ID.

    Args:
        id: Identifier of service to be registered/updated.

    Returns:
        Identifier of registered/updated service.
    """
    request_json = request.json
    if isinstance(request_json, dict):
        service = RegisterService(
            data=request_json,
            id=serviceId,
        )
        service.register_metadata()
        return service.data["id"]
    else:
        logger.error("Invalid request payload.")
        raise BadRequest


# POST /service-info
@log_traffic
def postServiceInfo(**kwargs) -> tuple[None, int, dict]:
    """Set information about this service.

    Returns:
        An empty 201 response with headers.
    """
    request_json = request.json
    if isinstance(request_json, dict):
        service_info = RegisterServiceInfo()
        headers = service_info.set_service_info_from_app_context(data=request_json)
        return None, 201, headers
    else:
        logger.error("Invalid request payload.")
        raise BadRequest
