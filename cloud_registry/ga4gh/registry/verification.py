"""Controllers for verifications of services.

Admins verify services they checked (e.g., that they accept LS Login tokens).
The time of the verification is stored in an internal field of the service
(`VERIFIED`), not served with the service, so that the GA4GH endpoints are
unchanged. Replacing a service with another URL (as written) or API type
removes it (cf. `RegisterService._replace()`), deleting a service deletes it
with the service. Verifications are served with the URL and API type of the
service, read from the same document, so that clients can match them against
the services they list.

Only admins may verify services and remove verifications: no policy in
`config.yaml` allows these operations to other users. Verifications can be
read by anyone.
"""

from datetime import datetime, timezone
import logging

from flask import Response
from foca.security.access_control import get_current_subject
from pymongo import ReturnDocument

from cloud_registry.exceptions import Conflict, NotFound
from cloud_registry.ga4gh.registry.server import problem
from cloud_registry.ga4gh.registry.service import (
    KEY,
    VERIFIED,
    InvalidService,
    service_exists,
    service_key,
)
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)
# Projection of the fields served with verifications
VERIFICATION_PROJECTION = {"_id": False, "url": True, "type": True, VERIFIED: True}


class VerificationConflict(Conflict):
    """The service has another URL or API type than the one checked."""

    def __init__(self) -> None:
        super().__init__(
            "The service has another URL or API type than the one checked; "
            "check the service as it is registered, then verify it again."
        )


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
        service: Stored service, with fields `url`, `type` and `VERIFIED`.

    Returns:
        Verification, with the URL and API type of the service and the time
        of the verification as `verified_at`.
    """
    return {
        "url": service["url"],
        "type": service["type"],
        "verified_at": _format_time(service[VERIFIED]),
    }


def _checked_filter(checked: dict) -> dict | None:
    """Get the filter for a service with the URL and API type an admin
    checked.

    Args:
        checked: URL and API type the admin checked (cf. request body of
            `putServiceVerification()`).

    Returns:
        Filter for services with exactly that URL, as written, and the same
        API type (cf. `service_key()`; the version is not compared), or
        `None` if the URL is invalid, as no service has it then.
    """
    try:
        key = service_key(checked)
    except InvalidService:
        return None
    return {"url": checked["url"], KEY: key}


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
        {"id": serviceId, VERIFIED: {"$exists": True}}, VERIFICATION_PROJECTION
    )
    if service is None:
        raise NotFound
    return _verification(service)


# PUT /services/{serviceId}/verification
def putServiceVerification(
    serviceId: str, verification: dict | None = None, **kwargs
) -> dict | Response:
    """Verify a service, or renew its verification.

    If the request names the URL and API type the admin checked, the service
    is only verified if it has them (checked in the same write), so that a
    service changed after the admin checked it isn't verified.

    Args:
        serviceId: Service identifier.
        verification: URL and API type the admin checked; optional.

    Returns:
        Verification of the service, at the current time; or a problem
        details response (`409`) if the service has another URL or API type
        than the one checked.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist.
    """
    filter: dict | None = {"id": serviceId}
    if verification is not None:
        checked = _checked_filter(verification)
        filter = None if checked is None else {"id": serviceId, **checked}
    verified_at = _now()
    service = None
    if filter is not None:
        service = get_collection("services").find_one_and_update(
            filter,
            {"$set": {VERIFIED: verified_at}},
            projection=VERIFICATION_PROJECTION,
            return_document=ReturnDocument.AFTER,
        )
    if service is None:
        if not service_exists(serviceId):
            raise NotFound
        exc = VerificationConflict()
        logger.info("Service '%s' not verified: %s", serviceId, exc.description)
        return problem(exc)
    logger.info("Service '%s' verified by '%s'.", serviceId, get_current_subject())
    return _verification(service)


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
        {**VERIFICATION_PROJECTION, "id": True},
        sort=[("id", 1)],
    )
    return [
        {"service_id": service["id"], **_verification(service)} for service in services
    ]
