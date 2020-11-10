"""Controllers for DRS endpoints."""

import logging
from typing import (Dict, List, Tuple)

from flask import (request, current_app)
from foca.utils.logging import log_traffic

from cloud_registry.ga4gh.registry.service_info import (
    RegisterServiceInfo,
)

logger = logging.getLogger(__name__)


@log_traffic
def getServices() -> List:
    """List all services

    Returns:
        List of services.
    """
    db_collection_service = (
        current_app.config['FOCA'].db.dbs['serviceStore']
        .collections['services'].client
    )
    records = db_collection_service.find(
        filter={},
        projection={"_id": False},
    )
    return list(records)


@log_traffic
def getServiceById(serviceId: str) -> Dict:
    """"""
    return {}


@log_traffic
def getServiceTypes() -> List:
    """"""
    return []


@log_traffic
def getServiceInfo() -> Dict:
    """Show information about this service.

    Returns:
        An empty 201 response with headers.
    """
    service_info = RegisterServiceInfo()
    return service_info.get_service_info()


@log_traffic
def postServiceInfo() -> Tuple[None, str, Dict]:
    """Show information about this service.

    Returns:
        An empty 201 response with headers.
    """
    service_info = RegisterServiceInfo()
    headers = service_info.set_service_info_from_app_context(data=request.json)
    return None, '201', headers
