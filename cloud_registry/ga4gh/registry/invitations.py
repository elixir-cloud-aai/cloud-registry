"""Controllers for invitations to services by link.

Owners of a service (and admins) invite users without knowing their user
identifiers: they create an invitation for a role (`POST
/services/{serviceId}/invitations`), which returns a one-time token, and send
a link with it through any channel; the invited user logs in and accepts it
(`POST /invitations/accept`), which grants the role to their own identifier,
as `PUT /services/{serviceId}/permissions/{userId}` would (replacing their
other role on the service). Owners list the invitations of a service, with
who accepted them, and revoke open ones.

Invitations are stored in their own collection (`INVITATIONS`), with the
SHA-256 hash of their token, never the token itself, so that the stored
invitations can't be accepted by those who read the database. They expire
after `custom.access_control.invitations.expiry_days` (default: 7), also once
accepted, and are then deleted by MongoDB (TTL index on `expires_at`; until
then, reads skip them). A service has at most
`custom.access_control.invitations.max_open_per_service` open invitations
(neither accepted nor expired; default: 20); concurrent requests can exceed
it by their number.

Accepting is single-use: the invitation is claimed atomically (an update of
an open, unexpired invitation), and released again if the role can't be
granted (e.g., the user is the last owner of the service, and the role is
`maintainer`), so that it can still be used. Invitations are deleted with the
roles on their service (cf. `remove_permissions()`), i.e., when the service
is deleted, and move with them when it is renamed (cf.
`cloud_registry.ga4gh.registry.rename`).

A forwarded link grants the role to whoever accepts it first; expiry, single
use, and owners seeing who accepted (and being able to revoke the role)
limit this.
"""

from datetime import timedelta
import hashlib
import logging
import secrets
from typing import Any
from uuid import uuid4

from flask import Response
from foca import get_custom_config
from foca.utils.logging import escape_log_value

from cloud_registry.exceptions import CodedConflict, NotFound, problem
from cloud_registry.ga4gh.registry.permissions import (
    INVITATIONS,
    current_user,
    get_current_claims,
    put_role,
    remember_current_user,
    require_subject,
)
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import format_time, get_collection, now

logger = logging.getLogger(__name__)
# Fields of stored invitations: the hash of the token, and the acceptance
TOKEN_HASH = "token_hash"  # noqa: S105 (field name)
ACCEPTED = "accepted"
# Bytes of randomness of tokens (cf. `secrets.token_urlsafe()`)
TOKEN_BYTES = 32


class InvitationAccepted(CodedConflict):
    """The invitation was accepted already."""

    problem_code = "invitation-accepted"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "The invitation was accepted already; ask an owner of the service "
            "for a new one, or to revoke the role instead."
        )


class InvitationsLimit(CodedConflict):
    """A service has as many open invitations as allowed."""

    problem_code = "invitations-limit"

    def __init__(self, limit: int) -> None:
        """Create the exception, with a description of what to do.

        Args:
            limit: How many open invitations a service may have.
        """
        super().__init__(
            f"The service has {limit} open invitations, as many as allowed; "
            "revoke some, or wait until they are accepted or expire."
        )


def hash_token(token: str) -> str:
    """Hash the token of an invitation, as it is stored.

    Args:
        token: Token of an invitation.

    Returns:
        SHA-256 hash of the token (hex digest).
    """
    return hashlib.sha256(token.encode()).hexdigest()


def _open_filter() -> dict[str, Any]:
    """Get the filter for open invitations.

    Returns:
        Filter for invitations that are neither accepted nor expired.
    """
    return {ACCEPTED: {"$exists": False}, "expires_at": {"$gt": now()}}


def _serve(invitation: dict[str, Any]) -> dict[str, Any]:
    """Get an invitation as served.

    Args:
        invitation: Stored invitation.

    Returns:
        The invitation, without the hash of its token, with times as RFC 3339
        date-times.
    """
    served = {
        "id": invitation["_id"],
        "service_id": invitation["service_id"],
        "role": invitation["role"],
        "created_by": invitation["created_by"],
        "created_at": format_time(invitation["created_at"]),
        "expires_at": format_time(invitation["expires_at"]),
    }
    accepted = invitation.get(ACCEPTED)
    if accepted is not None:
        served[ACCEPTED] = {
            key: value for key, value in accepted.items() if key != "accepted_at"
        } | {"accepted_at": format_time(accepted["accepted_at"])}
    return served


def _check_limit(serviceId: str) -> None:
    """Check that a service may have another open invitation.

    Args:
        serviceId: Service identifier.

    Raises:
        InvitationsLimit: The service has as many open invitations as allowed
            (`custom.access_control.invitations.max_open_per_service`).
    """
    limit = get_custom_config(
        CustomConfig
    ).access_control.invitations.max_open_per_service
    if limit is None:
        return
    query = {"service_id": serviceId, **_open_filter()}
    # a limit of 0 counts all documents
    if limit == 0 or (
        get_collection(INVITATIONS).count_documents(query, limit=limit) >= limit
    ):
        raise InvitationsLimit(limit)


# POST /services/{serviceId}/invitations
def postServiceInvitation(
    serviceId: str, invitation: dict[str, Any], **kwargs: Any
) -> Response | tuple[dict[str, Any], int]:
    """Create an invitation to a service.

    Args:
        serviceId: Service identifier.
        invitation: Request body, with the role to grant as `role`.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The invitation (cf. `_serve()`), with its token (`token`), only
        returned here; with status `201`. Or an error response: `409` if the
        service has as many open invitations as allowed.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, or was
            deleted meanwhile.
    """
    creator = require_subject()
    if not service_exists(serviceId):
        raise NotFound
    try:
        _check_limit(serviceId)
    except InvitationsLimit as exc:
        logger.info(
            "Invitation to service '%s' not created: %s",
            escape_log_value(serviceId),
            exc.description,
        )
        return problem(exc)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    created_at = now()
    expiry_days = get_custom_config(CustomConfig).access_control.invitations.expiry_days
    document = {
        "_id": uuid4().hex,
        TOKEN_HASH: hash_token(token),
        "service_id": serviceId,
        "role": invitation["role"],
        "created_by": creator,
        "created_at": created_at,
        "expires_at": created_at + timedelta(days=expiry_days),
    }
    collection = get_collection(INVITATIONS)
    collection.insert_one(document)
    # the service may have been deleted (with its invitations) meanwhile
    if not service_exists(serviceId):
        collection.delete_one({"_id": document["_id"]})
        raise NotFound
    logger.info(
        "Invitation '%s' to service '%s' as '%s' created by '%s'.",
        document["_id"],
        escape_log_value(serviceId),
        document["role"],
        current_user(),
    )
    return _serve(document) | {"token": token}, 201


# GET /services/{serviceId}/invitations
def getServiceInvitations(serviceId: str, **kwargs: Any) -> list[dict[str, Any]]:
    """List the invitations to a service.

    Args:
        serviceId: Service identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The open and accepted invitations of the service that haven't
        expired (cf. `_serve()`), without tokens, oldest first.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist.
    """
    if not service_exists(serviceId):
        raise NotFound
    invitations = get_collection(INVITATIONS).find(
        {"service_id": serviceId, "expires_at": {"$gt": now()}},
        sort=[("created_at", 1), ("_id", 1)],
    )
    return [_serve(invitation) for invitation in invitations]


# DELETE /services/{serviceId}/invitations/{invitationId}
def deleteServiceInvitation(
    serviceId: str, invitationId: str, **kwargs: Any
) -> str | Response:
    """Revoke an open invitation to a service.

    Accepted invitations stay listed until they expire; revoke the role
    instead (`DELETE /services/{serviceId}/permissions/{userId}`).

    Args:
        serviceId: Service identifier.
        invitationId: Invitation identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier of the revoked invitation; or an error response: `409` if
        it was accepted.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, or the
            invitation doesn't exist, is of another service or expired.
    """
    if not service_exists(serviceId):
        raise NotFound
    collection = get_collection(INVITATIONS)
    query = {"_id": invitationId, "service_id": serviceId}
    result = collection.delete_one({**query, **_open_filter()})
    if not result.deleted_count:
        if collection.count_documents(
            {**query, ACCEPTED: {"$exists": True}, "expires_at": {"$gt": now()}},
            limit=1,
        ):
            return problem(InvitationAccepted())
        raise NotFound
    logger.info(
        "Invitation '%s' to service '%s' revoked by '%s'.",
        escape_log_value(invitationId),
        escape_log_value(serviceId),
        current_user(),
    )
    return invitationId


def _acceptance(user_id: str) -> dict[str, Any]:
    """Describe the acceptance of an invitation by the current user.

    Args:
        user_id: Identifier of the current user.

    Returns:
        User identifier, time, and the user's display name (`name`) and email
        address (`email`) if the identity provider releases them (cf.
        `security.auth.userinfo_claims`), so that owners see who accepted.
    """
    claims = get_current_claims()
    acceptance: dict[str, Any] = {"user_id": user_id, "accepted_at": now()}
    for claim in ("name", "email"):
        value = claims.get(claim)
        if isinstance(value, str) and value.strip():
            acceptance[claim] = value.strip()
    return acceptance


# POST /invitations/accept
def acceptInvitation(acceptance: dict[str, Any], **kwargs: Any) -> Any:
    """Accept an invitation, which grants its role to the current user.

    See `put_role()`, which grants the role (replacing the user's other role
    on the service). The invitation is claimed first, so that it is used
    once, also by concurrent requests, and released if the role can't be
    granted. The user's display name is stored for the service (cf.
    `remember_current_user()`).

    Args:
        acceptance: Request body, with the token of the invitation as
            `token`.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Identifier of the service (`service_id`) and the granted role
        (`role`); or an error response: `409` if the invitation was accepted
        already.

    Raises:
        cloud_registry.exceptions.NotFound: No open invitation has the token
            (unknown, revoked or expired), or its service doesn't exist.
        Exception: The role can't be granted (cf. `put_role()`), e.g.,
            `LastOwner`; the invitation is released.
    """
    user_id = require_subject()
    token_hash = hash_token(acceptance["token"])
    collection = get_collection(INVITATIONS)
    invitation = collection.find_one_and_update(
        {TOKEN_HASH: token_hash, **_open_filter()},
        {"$set": {ACCEPTED: _acceptance(user_id)}},
    )
    if invitation is None:
        accepted = {TOKEN_HASH: token_hash, ACCEPTED: {"$exists": True}}
        if collection.count_documents(
            {**accepted, "expires_at": {"$gt": now()}}, limit=1
        ):
            return problem(InvitationAccepted())
        raise NotFound
    service_id: str = invitation["service_id"]
    role: str = invitation["role"]
    try:
        put_role(service_id, user_id, "user", role)
    except Exception:
        # usable again, unless deleted meanwhile (e.g., with the service)
        collection.update_one(
            {"_id": invitation["_id"], f"{ACCEPTED}.user_id": user_id},
            {"$unset": {ACCEPTED: ""}},
        )
        raise
    remember_current_user([service_id])
    logger.info(
        "Invitation '%s' to service '%s' as '%s' accepted by '%s'.",
        invitation["_id"],
        escape_log_value(service_id),
        role,
        current_user(),
    )
    return {"service_id": service_id, "role": role}
