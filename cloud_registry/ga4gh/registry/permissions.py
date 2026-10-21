"""Access control: policies, service ownership and controllers for managing
permissions on services.

Changes of roles are logged (who changed which role of whom on which
service), for auditing.

Policies are configured in `config.yaml` (`security.access_control`) and
enforced by FOCA on all operations that require authentication, using the
model in `cloud_registry/access_control/model.conf`. Subjects with the role
`admin` (mapped from group claims via `security.access_control.group_mapping`,
which deployments set) may do everything.
"""

import logging

from flask import current_app, request
from flask.globals import request_ctx
from foca import get_config
from foca.security.access_control import (
    add_resource_role,
    get_current_groups,
    get_current_subject,
    list_resource_roles,
    remove_resource,
    remove_resource_role,
)
from foca.security.access_control.enforcement import (
    RESOURCE_ROLE_PTYPE,
    get_enforcer,
)

from cloud_registry.exceptions import BadRequest, Conflict, NotFound
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.utils import escape_log_value, get_collection, get_custom_config

logger = logging.getLogger(__name__)

# Roles that can be granted on services
ROLES = ["owner", "maintainer"]
# Collection with the display names of users with roles on services
USER_NAMES = "user_names"


def check_admin_groups() -> None:
    """Check that groups are mapped to the role `admin`, and log them.

    Without such groups, nobody can manage services they don't own, register
    services with chosen identifiers or verify services, so this is an error
    unless `custom.access_control.require_admin` is false.

    Raises:
        RuntimeError: No group is mapped to the role `admin`, and
            `custom.access_control.require_admin` is true.
    """
    mapping = get_config().security.access_control.group_mapping or {}
    groups = sorted(group for group, role in mapping.items() if role == "admin")
    if groups:
        logger.info("Groups mapped to the role 'admin': %s", ", ".join(groups))
    elif get_custom_config().access_control.require_admin:
        raise RuntimeError(
            "No group is mapped to the role 'admin': set "
            "`security.access_control.group_mapping` (e.g., to map an admin "
            "group of the identity provider to 'admin'), or set "
            "`custom.access_control.require_admin` to false to run without "
            "admins"
        )
    else:
        logger.warning(
            "No group is mapped to the role 'admin', so nobody can manage "
            "services they don't own, register services with chosen "
            "identifiers or verify services"
        )


def _api_base_path() -> str:
    """Get the base path of the API handling the current request.

    Returns:
        Base path, e.g., `/ga4gh/registry/v1` (Connexion registers each API
        as a Flask blueprint with its base path as URL prefix).
    """
    return current_app.blueprints[request.blueprint].url_prefix or ""  # type: ignore[index]


def service_path(serviceId: str) -> str:
    """Get resource path of a service, i.e., the path roles are granted on.

    Args:
        serviceId: Service identifier.

    Returns:
        Request path of the service, including the base path of the API
        handling the current request.
    """
    return f"{_api_base_path()}/services/{serviceId}"


def _get_current_claims() -> dict:
    """Get the claims of the validated token of the current request.

    Returns:
        Claims, including those added from user info (cf.
        `security.auth.userinfo_claims`), or an empty dictionary.
    """
    # Connexion stores the validated token info on Flask's request context;
    # FOCA's `validate_token()` returns the claims under key `claims`
    context = getattr(request_ctx, "connexion_context", None) or {}
    token_info = context.get("token_info")
    if not isinstance(token_info, dict):
        return {}
    claims = token_info.get("claims", token_info)
    return claims if isinstance(claims, dict) else {}


def current_user() -> str:
    """Get the subject of the current request, for log messages.

    Returns:
        User identifier, with non-printable characters escaped.
    """
    return escape_log_value(get_current_subject())


def is_admin() -> bool:
    """Check whether the subject of the current request is an admin.

    Returns:
        Whether the subject has the role `admin`, as the policies see it:
        from group claims (cf. `security.access_control.group_mapping`) or
        granted to the user via FOCA's permissions API (a `g` rule).
    """
    prefix = get_config().security.access_control.subject_prefix
    role_manager = get_enforcer().get_role_manager()
    subjects = [f"{prefix}{get_current_subject()}", *get_current_groups()]
    return any(role_manager.has_link(subject, "admin") for subject in subjects)


def remember_current_user(service_ids: list[str]) -> None:
    """Store the display name of the subject of the current request for
    services they have a role on.

    Names are shown to owners and admins listing the roles on a service, so
    they are only stored per service, once the user has used the registry
    with a role on it (and are removed with the role). Nothing is stored if
    the identity provider reports no name.

    Args:
        service_ids: Identifiers of services the user has a role on.
    """
    user_id = get_current_subject()
    name = _get_current_claims().get("name")
    if user_id is None or not isinstance(name, str) or not name.strip():
        return
    for service_id in service_ids:
        get_collection(USER_NAMES).update_one(
            {"service_id": service_id, "user_id": user_id},
            {"$set": {"name": name.strip()}},
            upsert=True,
        )


def _forget_names(serviceId: str, userId: str | None = None) -> None:
    """Delete display names stored for users on a service (cf.
    `remember_current_user()`).

    Args:
        serviceId: Service identifier.
        userId: User identifier; names of all users if `None`.
    """
    query = {"service_id": serviceId}
    if userId is not None:
        query["user_id"] = userId
    get_collection(USER_NAMES).delete_many(query)


def add_owner(serviceId: str) -> None:
    """Make the subject of the current request owner of a service.

    Args:
        serviceId: Service identifier.
    """
    add_resource_role(
        subject=get_current_subject(),  # type: ignore[arg-type]
        role="owner",
        resource=service_path(serviceId),
    )
    logger.info(
        "Role 'owner' on new service '%s' granted to its creator '%s'.",
        escape_log_value(serviceId),
        current_user(),
    )
    remember_current_user([serviceId])


def remove_owner(serviceId: str) -> None:
    """Revoke the owner role of the subject of the current request on a
    service, and their name stored for it (e.g., if the service they
    registered was deleted meanwhile). Other roles on the service (e.g., of
    the owner of a service registered again with the identifier) are kept.

    Args:
        serviceId: Service identifier.
    """
    user_id: str = get_current_subject()  # type: ignore[assignment]
    if remove_resource_role(
        subject=user_id, role="owner", resource=service_path(serviceId)
    ):
        logger.info(
            "Role 'owner' on deleted service '%s' revoked from its creator '%s'.",
            escape_log_value(serviceId),
            current_user(),
        )
    _forget_names(serviceId, user_id)


def remove_permissions(serviceId: str) -> None:
    """Revoke all roles granted on a service.

    Args:
        serviceId: Service identifier.
    """
    if remove_resource(service_path(serviceId)):
        logger.info(
            "All roles on service '%s' revoked by '%s'.",
            escape_log_value(serviceId),
            current_user(),
        )
    _forget_names(serviceId)


# GET /services/{serviceId}/permissions
def getServicePermissions(serviceId: str, **kwargs) -> list[dict]:
    """List roles granted on a service.

    Args:
        serviceId: Service identifier.

    Returns:
        User identifier, display name (if known) and role of each role
        granted on the service.
    """
    if not service_exists(serviceId):
        raise NotFound
    roles = list_resource_roles(service_path(serviceId))
    names = {
        user["user_id"]: user["name"]
        for user in get_collection(USER_NAMES).find({"service_id": serviceId})
    }
    return [
        {"user_id": user_id, "role": role}
        | ({"name": names[user_id]} if user_id in names else {})
        for user_id, role in roles
    ]


def _owners(resource: str) -> list[str]:
    """Get the owners of a service.

    Args:
        resource: Resource path of the service.

    Returns:
        User identifiers of the owners.
    """
    return [user for user, role in list_resource_roles(resource) if role == "owner"]


def _is_owner(resource: str, userId: str) -> bool:
    """Check whether a user is an owner of a service.

    Args:
        resource: Resource path of the service.
        userId: User identifier.

    Returns:
        Whether the user is an owner.
    """
    return userId in _owners(resource)


def _check_other_owners(resource: str, userId: str) -> None:
    """Check that a service has owners other than a user.

    Args:
        resource: Resource path of the service.
        userId: User identifier.

    Raises:
        cloud_registry.exceptions.Conflict: The user is the only owner of
            the service.
    """
    if _owners(resource) == [userId]:
        logger.info(
            "User '%s' is the last owner of '%s'",
            escape_log_value(userId),
            escape_log_value(resource),
        )
        raise Conflict


def _grant(serviceId: str, resource: str, userId: str, role: str) -> None:
    """Grant a user a role on a service, unless the service was deleted.

    Checking that the service exists before granting the role is not atomic:
    the service may be deleted in between (after its roles were revoked),
    and the role would be left on the deleted service. So the existence is
    checked again afterwards, and if the service is gone, the role (and a
    name stored for the user on the service) is revoked again. (A deletion
    also revokes the roles again after deleting the service, cf.
    `deleteService()`.)

    Args:
        serviceId: Service identifier.
        resource: Resource path of the service.
        userId: User identifier.
        role: Role to grant.

    Raises:
        cloud_registry.exceptions.NotFound: The service was deleted
            meanwhile.
        ValueError: The user identifier can't be stored in policy rules.
    """
    add_resource_role(subject=userId, role=role, resource=resource)
    if not service_exists(serviceId):
        remove_resource_role(subject=userId, role=role, resource=resource)
        _forget_names(serviceId, userId)
        raise NotFound


def _restore_owner_if_none(serviceId: str, resource: str, userId: str) -> None:
    """Make a former owner owner again if a service has no owners left.

    Checking for other owners before changing roles is not atomic:
    concurrent requests of owners removing each other could leave a service
    without owners. Each request therefore checks again afterwards and, if
    no owner is left, restores the owner role it removed.

    Args:
        serviceId: Service identifier.
        resource: Resource path of the service.
        userId: Identifier of the user whose owner role was removed.

    Raises:
        cloud_registry.exceptions.Conflict: The service had no owners left.
        cloud_registry.exceptions.NotFound: The service was deleted
            meanwhile.
    """
    if not _owners(resource):
        # A concurrent deletion of the service removed all roles, unless
        # the service still exists afterwards
        _grant(serviceId, resource, userId, "owner")
        logger.info(
            "Restored owner '%s' of '%s', removed concurrently by '%s'",
            escape_log_value(userId),
            escape_log_value(resource),
            current_user(),
        )
        raise Conflict


# PUT /services/{serviceId}/permissions/{userId}
def putServicePermission(serviceId: str, userId: str, **kwargs) -> dict:
    """Grant a user a role on a service, replacing any other role.

    Other roles are revoked before the role is granted, so that a failure
    leaves the user with fewer rights, not more. On a user's first role on
    the service, a name stored for them on it is deleted (cf.
    `remember_current_user()`).

    Args:
        serviceId: Service identifier.
        userId: User identifier (`sub` claim of the user's tokens).

    Returns:
        User identifier and granted role.

    Raises:
        cloud_registry.exceptions.BadRequest: The user identifier can't be
            stored in policy rules.
        cloud_registry.exceptions.Conflict: The user is the last owner of
            the service, and the role is not `owner`.
        cloud_registry.exceptions.NotFound: The service doesn't exist, or
            was deleted meanwhile (cf. `_grant()`).
    """
    role = request.json["role"]
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    previous_roles = [
        other_role
        for user, other_role in list_resource_roles(resource)
        if user == userId
    ]
    if role != "owner":
        _check_other_owners(resource, userId)
    try:
        for other_role in ROLES:
            if other_role != role:
                remove_resource_role(subject=userId, role=other_role, resource=resource)
        if "owner" in previous_roles and role != "owner":
            _restore_owner_if_none(serviceId, resource, userId)
        if not previous_roles:
            _forget_names(serviceId, userId)
        _grant(serviceId, resource, userId, role)
    # FOCA rejects values that can't be stored in policy rules (e.g., user
    # identifiers with commas)
    except ValueError as exc:
        logger.info("Invalid permission: %s", escape_log_value(exc))
        raise BadRequest from exc
    logger.info(
        "Role '%s' on service '%s' granted to '%s' by '%s' (previous roles: %s).",
        role,
        escape_log_value(serviceId),
        escape_log_value(userId),
        current_user(),
        ", ".join(previous_roles) or "none",
    )
    return {"user_id": userId, "role": role}


# DELETE /services/{serviceId}/permissions/{userId}
def deleteServicePermission(serviceId: str, userId: str, **kwargs) -> str:
    """Revoke all roles of a user on a service.

    Args:
        serviceId: Service identifier.
        userId: User identifier.

    Returns:
        User identifier.

    Raises:
        cloud_registry.exceptions.Conflict: The user is the last owner of
            the service.
    """
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    _check_other_owners(resource, userId)
    was_owner = _is_owner(resource, userId)
    revoked = False
    for role in ROLES:
        try:
            revoked |= remove_resource_role(
                subject=userId, role=role, resource=resource
            )
        # FOCA rejects values that can't be in policy rules, so no role
        # exists for them
        except ValueError as exc:
            raise NotFound from exc
    if not revoked:
        raise NotFound
    if was_owner:
        _restore_owner_if_none(serviceId, resource, userId)
    _forget_names(serviceId, userId)
    logger.info(
        "Roles on service '%s' of '%s' revoked by '%s'.",
        escape_log_value(serviceId),
        escape_log_value(userId),
        current_user(),
    )
    return userId


# GET /me
def getMe(**kwargs) -> dict:
    """Get the current user, and store or refresh their display name for
    the services they have a role on.

    Returns:
        User identifier, whether the user is an admin (cf. `is_admin()`),
        and the user's roles on services, sorted by service identifier.
    """
    user_id: str = get_current_subject()  # type: ignore[assignment]
    prefix = get_config().security.access_control.subject_prefix
    services_path = f"{_api_base_path()}/services/"
    services = []
    for _, role, resource in get_enforcer().get_filtered_named_grouping_policy(
        RESOURCE_ROLE_PTYPE, 0, f"{prefix}{user_id}"
    ):
        service_id = resource.removeprefix(services_path)
        if resource.startswith(services_path) and "/" not in service_id:
            services.append({"service_id": service_id, "role": role})
    remember_current_user([service["service_id"] for service in services])
    return {
        "user_id": user_id,
        "admin": is_admin(),
        "services": sorted(services, key=lambda service: service["service_id"]),
    }
