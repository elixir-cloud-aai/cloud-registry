"""Access control: policies, service ownership and controllers for managing
permissions on services.

Policies are configured in `config.yaml` (`security.access_control`) and
enforced by FOCA on all operations that require authentication, using the
model in `cloud_registry/access_control/model.conf`. Subjects with the role
`admin` (mapped from group claims via `security.access_control.group_mapping`,
which deployments set) may do everything.
"""

import logging

from flask import request
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
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)

# Roles that can be granted on services
ROLES = ["owner", "maintainer"]
# Collection with the display names of users
USERS = "users"


def check_admin_groups() -> list[str]:
    """Check that groups are mapped to the role `admin`, and log them.

    Without such groups, nobody can manage services they don't own or set the
    service info, so this is an error unless
    `custom.access_control.require_admin` is false.

    Returns:
        Groups mapped to the role `admin`, sorted.

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
            "services they don't own or set the service info"
        )
    return groups


def service_path(serviceId: str) -> str:
    """Get resource path of a service, i.e., the path roles are granted on.

    Args:
        serviceId: Service identifier.

    Returns:
        Request path of the service, including the base path of the API
        handling the current request.
    """
    base_path = request.path.split("/services", 1)[0]
    return f"{base_path}/services/{serviceId}"


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


def remember_current_user() -> None:
    """Store the display name of the subject of the current request.

    Names are shown to owners and admins listing the roles on a service.
    Nothing is stored if the identity provider reports no name.
    """
    user_id = get_current_subject()
    name = _get_current_claims().get("name")
    if user_id is None or not isinstance(name, str) or not name.strip():
        return
    get_collection(USERS).update_one(
        {"user_id": user_id}, {"$set": {"name": name.strip()}}, upsert=True
    )


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
    remember_current_user()


def remove_permissions(serviceId: str) -> None:
    """Revoke all roles granted on a service.

    Args:
        serviceId: Service identifier.
    """
    remove_resource(service_path(serviceId))


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
        for user in get_collection(USERS).find(
            {"user_id": {"$in": [user_id for user_id, _ in roles]}}
        )
    }
    return [
        {"user_id": user_id, "role": role}
        | ({"name": names[user_id]} if user_id in names else {})
        for user_id, role in roles
    ]


def _check_other_owners(resource: str, userId: str) -> None:
    """Check that a service has owners other than a user.

    Args:
        resource: Resource path of the service.
        userId: User identifier.

    Raises:
        cloud_registry.exceptions.Conflict: The user is the only owner of
            the service.
    """
    owners = [user for user, role in list_resource_roles(resource) if role == "owner"]
    if owners == [userId]:
        logger.info("User '%s' is the last owner of '%s'", userId, resource)
        raise Conflict


# PUT /services/{serviceId}/permissions/{userId}
def putServicePermission(serviceId: str, userId: str, **kwargs) -> dict:
    """Grant a user a role on a service, replacing any other role.

    Args:
        serviceId: Service identifier.
        userId: User identifier (`sub` claim of the user's tokens).

    Returns:
        User identifier and granted role.

    Raises:
        cloud_registry.exceptions.Conflict: The user is the last owner of
            the service, and the role is not `owner`.
    """
    role = request.json["role"]
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    if role != "owner":
        _check_other_owners(resource, userId)
    try:
        add_resource_role(subject=userId, role=role, resource=resource)
    except ValueError as exc:
        logger.info("Invalid permission: %s", exc)
        raise BadRequest from exc
    for other_role in ROLES:
        if other_role != role:
            remove_resource_role(subject=userId, role=other_role, resource=resource)
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
    revoked = False
    for role in ROLES:
        try:
            revoked |= remove_resource_role(
                subject=userId, role=role, resource=resource
            )
        except ValueError as exc:
            raise NotFound from exc
    if not revoked:
        raise NotFound
    return userId


# GET /me
def getMe(**kwargs) -> dict:
    """Get the current user and refresh their display name.

    Returns:
        User identifier, whether the user is an admin, and the user's roles
        on services, sorted by service identifier.
    """
    user_id: str = get_current_subject()  # type: ignore[assignment]
    remember_current_user()
    prefix = get_config().security.access_control.subject_prefix
    services_path = f"{request.path.rsplit('/me', 1)[0]}/services/"
    services = []
    for _, role, resource in get_enforcer().get_filtered_named_grouping_policy(
        RESOURCE_ROLE_PTYPE, 0, f"{prefix}{user_id}"
    ):
        service_id = resource.removeprefix(services_path)
        if resource.startswith(services_path) and "/" not in service_id:
            services.append({"service_id": service_id, "role": role})
    return {
        "user_id": user_id,
        "admin": "admin" in get_current_groups(),
        "services": sorted(services, key=lambda service: service["service_id"]),
    }
