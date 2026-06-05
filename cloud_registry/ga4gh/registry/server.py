"""Controllers for service endpoints."""

import logging
from math import ceil
from typing import Dict, List, Tuple

from cloud_registry.exceptions import BadRequest, NotFound
from cloud_registry.ga4gh.registry.service import RegisterService
from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo
from flask import current_app, request
from foca.utils.logging import log_traffic

logger = logging.getLogger(__name__)


# GET /services
@log_traffic
def getServices(**kwargs) -> List:
    """List all services.

    Returns:
        List of services or Paginated list of services.
    """

    # Get pagination data
    page = request.args.get("page", type=int)
    page_size = request.args.get("page_size", type=int)

    foca_conf = current_app.config.foca  # type: ignore[attr-defined]
    db_collection_service = (
        foca_conf.db.dbs["serviceStore"].collections["services"].client
    )

    # return list if no pagination query found
    if page is None and page_size is None:
        records = db_collection_service.find(
            filter={},
            projection={"_id": False},
        )
        return list(records)

    # Return paginated response
    page = page or 1
    page_size = page_size or 10
    total_count = db_collection_service.count_documents({})
    total_pages = ceil(total_count / page_size)

    if page < 1 or (total_count > 0 and page > total_pages):
        raise BadRequest

    skip_items = (page - 1) * page_size
    records = db_collection_service.find(
        filter={},
        projection={"_id": False},
    ).skip(skip_items).limit(page_size)

    return {
        "results": list(records),
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages
        }
    }


# GET /services/{serviceId}
@log_traffic
def getServiceById(serviceId: str, **kwargs) -> Dict:
    """Retrieve service by its identifier.

    Args:
        serviceId: Identifier of service to be retrieved.

    Returns:
        Service object.
    """
    foca_conf = current_app.config.foca  # type: ignore[attr-defined]
    db_collection_service = (
        foca_conf.db.dbs["serviceStore"].collections["services"].client
    )
    obj = db_collection_service.find_one({"id": serviceId})
    if not obj:
        raise NotFound
    del obj["_id"]
    return obj


# GET /services/types
@log_traffic
def getServiceTypes(**kwargs) -> List:
    """List types of services.

    Returns:
        List of distinct service types.
    """

    # Get pagination data
    page = request.args.get("page", type=int)
    page_size = request.args.get("page_size", type=int)

    services = getServices.__wrapped__()
    if isinstance(services, dict):
        services = services["results"]
    types = [s["type"] for s in services]
    uniq_types = [dict(t) for t in {tuple(sorted(d.items())) for d in types}]

    # return list if no pagination query found
    if page is None and page_size is None:
        return uniq_types

    # return paginated response
    page = page or 1
    page_size = page_size or 10
    total_count = len(uniq_types)
    total_pages = ceil(total_count / page_size)

    if page < 1 or (total_count > 0 and page > total_pages):
        raise BadRequest

    skip_items = (page - 1) * page_size
    paginated_types = uniq_types[skip_items:skip_items + page_size]

    return {
        "results": paginated_types,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages
        }
    }


# GET /service-info
@log_traffic
def getServiceInfo(**kwargs) -> Dict:
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
    foca_conf = current_app.config.foca  # type: ignore[attr-defined]
    db_collection_service = (
        foca_conf.db.dbs["serviceStore"].collections["services"].client
    )
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
def postServiceInfo(**kwargs) -> Tuple[None, str, Dict]:
    """Set information about this service.

    Returns:
        An empty 201 response with headers.
    """
    request_json = request.json
    if isinstance(request_json, dict):
        service_info = RegisterServiceInfo()
        headers = service_info.set_service_info_from_app_context(data=request_json)
        return None, "201", headers
    else:
        logger.error("Invalid request payload.")
        raise BadRequest
