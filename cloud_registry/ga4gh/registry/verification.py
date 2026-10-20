"""Controllers for verifications of services.

Admins verify services they checked (e.g., that they accept LS Login tokens).
The time of the verification is stored in an internal field of the service
(`VERIFIED`), not served with the service, so that the GA4GH endpoints are
unchanged. Replacing a service with another URL or API type removes it (cf.
`RegisterService._replace()`), deleting a service deletes it with the service.

Only admins may verify services and remove verifications: no policy in
`config.yaml` allows these operations to other users. Verifications can be
read by anyone.
"""

from datetime import datetime, timezone
import logging

from foca.security.access_control import get_current_subject

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.service import VERIFIED
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Get the current time, in the precision of the database.

    Returns:
        Current time (UTC), in milliseconds, like MongoDB dates.
    """
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def _format_time(time: datetime) -> str:
    """Format a time of a verification.

    Args:
        time: Time in UTC; naive, as read from the database, or aware.

    Returns:
        RFC 3339 date-time in UTC, in milliseconds, e.g.,
        `2026-10-20T12:34:56.789Z`.
    """
    return f"{time:%Y-%m-%dT%H:%M:%S}.{time.microsecond // 1000:03d}Z"


def _verification(service: dict) -> dict:
    """Get the verification of a stored service.

    Args:
        service: Stored service, with field `VERIFIED`.

    Returns:
        Verification, with the time of the verification as `verified_at`.
    """
    return {"verified_at": _format_time(service[VERIFIED])}


# GET /services/{serviceId}/verification
def getServiceVerification(serviceId: str, **kwargs) -> dict:
    """Get the verification of a service.

    Args:
        serviceId: Service identifier.

    Returns:
        Verification of the service.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist or is
            not verified.
    """
    service = get_collection("services").find_one(
        {"id": serviceId, VERIFIED: {"$exists": True}}, {"_id": False, VERIFIED: True}
    )
    if service is None:
        raise NotFound
    return _verification(service)


# PUT /services/{serviceId}/verification
def putServiceVerification(serviceId: str, **kwargs) -> dict:
    """Verify a service, or renew its verification.

    Args:
        serviceId: Service identifier.

    Returns:
        Verification of the service, at the current time.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist.
    """
    verified_at = _now()
    result = get_collection("services").update_one(
        {"id": serviceId}, {"$set": {VERIFIED: verified_at}}
    )
    if not result.matched_count:
        raise NotFound
    logger.info("Service '%s' verified by '%s'.", serviceId, get_current_subject())
    return _verification({VERIFIED: verified_at})


# DELETE /services/{serviceId}/verification
def deleteServiceVerification(serviceId: str, **kwargs) -> str:
    """Remove the verification of a service.

    Args:
        serviceId: Service identifier.

    Returns:
        Identifier of the service.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist or is
            not verified.
    """
    result = get_collection("services").update_one(
        {"id": serviceId, VERIFIED: {"$exists": True}}, {"$unset": {VERIFIED: ""}}
    )
    if not result.modified_count:
        raise NotFound
    logger.info(
        "Verification of service '%s' removed by '%s'.",
        serviceId,
        get_current_subject(),
    )
    return serviceId


# GET /verifications
def getVerifications(**kwargs) -> list[dict]:
    """List verified services.

    Returns:
        Identifier and verification of each verified service, sorted by
        identifier.
    """
    services = get_collection("services").find(
        {VERIFIED: {"$exists": True}},
        {"_id": False, "id": True, VERIFIED: True},
        sort=[("id", 1)],
    )
    return [
        {"service_id": service["id"], **_verification(service)} for service in services
    ]
