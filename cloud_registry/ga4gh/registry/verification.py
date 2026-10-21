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

from flask import Response, request
from foca.security.access_control import get_current_subject
from pymongo import ReturnDocument
from werkzeug.exceptions import HTTPException

from cloud_registry.exceptions import BadRequest, Conflict, NotFound, problem
from cloud_registry.ga4gh.registry.service import (
    KEY,
    VERIFIED,
    InvalidService,
    service_exists,
    service_key,
)
from cloud_registry.utils import escape_log_value, get_collection

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


def _checked_query(checked: dict) -> dict:
    """Get the query for a service with the URL and API type an admin
    checked.

    Args:
        checked: URL and API type the admin checked (cf. request body of
            `putServiceVerification()`).

    Returns:
        Query for services with exactly that URL, as written, and the same
        API type (cf. `service_key()`; the version is not compared).

    Raises:
        InvalidService: The URL is invalid.
    """
    return {"url": checked["url"], KEY: service_key(checked)}


def _has_unparsed_body() -> bool:
    """Check whether the current request has a body that wasn't parsed as
    JSON.

    Returns:
        Whether the request has a body other than none, an empty one or JSON
        `null`, e.g., malformed JSON or another media type, which Connexion
        passes to controllers as no body.
    """
    data = request.get_data()
    if not data:
        # form data is consumed when it is parsed (e.g., by Connexion, before
        # the controller), so a body was sent if it has a length, even if it
        # parsed to no fields (e.g., `&`)
        return bool(request.content_length) or bool(request.form or request.files)
    data = data.strip()
    return bool(data) and not (request.is_json and data == b"null")


def _not_verified(serviceId: str, exc: HTTPException) -> Response:
    """Log why a service is not verified, and build the error response.

    Args:
        serviceId: Service identifier.
        exc: Exception with a request-specific description.

    Returns:
        Problem details response.
    """
    logger.info(
        "Service '%s' not verified: %s",
        escape_log_value(serviceId),
        escape_log_value(exc.description),
    )
    return problem(exc)


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
        Verification of the service, at the current time (or the later time
        of a previous verification); or a problem details response: `400`
        if the body is neither JSON nor empty, or the URL in it is invalid;
        `409` if the service has another URL or API type than the one
        checked.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist.
    """
    query: dict = {"id": serviceId}
    if verification is not None:
        try:
            query.update(_checked_query(verification))
        except InvalidService as exc:
            return _not_verified(serviceId, exc)
    elif _has_unparsed_body():
        # not verified unconditionally if a precondition may have been meant
        return _not_verified(
            serviceId, BadRequest("The request body must be a JSON object or empty.")
        )
    service = get_collection("services").find_one_and_update(
        query,
        # renewals never move the time back (e.g., if clocks of instances
        # differ); the time stored is served
        {"$max": {VERIFIED: _now()}},
        projection=VERIFICATION_PROJECTION,
        return_document=ReturnDocument.AFTER,
    )
    if service is None:
        if not service_exists(serviceId):
            raise NotFound
        return _not_verified(serviceId, VerificationConflict())
    logger.info(
        "Service '%s' verified by '%s'.",
        escape_log_value(serviceId),
        escape_log_value(get_current_subject()),
    )
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
        escape_log_value(serviceId),
        escape_log_value(get_current_subject()),
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
