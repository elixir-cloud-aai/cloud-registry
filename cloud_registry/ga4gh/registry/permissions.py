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
from foca import get_config
from foca.security.access_control import (
    add_resource_role,
    get_current_subject,
    list_resource_roles,
    remove_resource,
    remove_resource_role,
)

from cloud_registry.exceptions import BadRequest, NotFound
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.utils import get_custom_config

logger = logging.getLogger(__name__)

# Roles that can be granted on services
ROLES = ["owner", "maintainer"]


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
        User identifier and role of each role granted on the service.
    """
    if not service_exists(serviceId):
        raise NotFound
    return [
        {"user_id": user_id, "role": role}
        for user_id, role in list_resource_roles(service_path(serviceId))
    ]


# PUT /services/{serviceId}/permissions/{userId}
def putServicePermission(serviceId: str, userId: str, **kwargs) -> dict:
    """Grant a user a role on a service, replacing any other role.

    Args:
        serviceId: Service identifier.
        userId: User identifier (`sub` claim of the user's tokens).

    Returns:
        User identifier and granted role.
    """
    role = request.json["role"]
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
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
    """
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
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
