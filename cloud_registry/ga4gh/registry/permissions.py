"""Access control: policies, service ownership and permission controllers.

The controllers manage permissions on services. Roles are granted to users (by
their identifier) or to groups (values of the group claim,
`security.access_control.claim_groups`); groups count like users, e.g., a group
may be the only owner of a service. Changes of roles are logged (who changed
which role of whom on which service), for auditing.

Policies are configured in `config.yaml` (`security.access_control`) and
enforced by FOCA on all operations that require authentication, using the
model in `cloud_registry/access_control/model.conf`. Subjects with the role
`admin` (mapped from group claims via `security.access_control.group_mapping`,
which deployments set) may do everything.
"""

import logging
from typing import Any

from flask import current_app, request
from flask.globals import request_ctx
from foca import get_config
from foca.security.access_control import (
    SubjectType,
    add_resource_role,
    get_current_group_values,
    get_current_groups,
    get_current_subject,
    list_resource_roles,
    remove_resource,
    remove_resource_role,
)
from foca.security.access_control.constants import GROUP_PREFIX
from foca.security.access_control.enforcement import (
    RESOURCE_ROLE_PTYPE,
    get_enforcer,
)
from foca.utils.logging import escape_log_value

from cloud_registry.exceptions import BadRequest, Conflict, Forbidden, NotFound
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)

# Roles that can be granted on services
ROLES = ["owner", "maintainer"]
# Types of subjects roles are granted to
SUBJECT_TYPES: tuple[SubjectType, ...] = ("user", "group")
# Collection with the display names of users with roles on services
USER_NAMES = "user_names"


def check_admin_groups() -> None:
    """Check that groups are mapped to the role `admin`, and log them.

    Without such groups, nobody can manage services they don't own, register
    services with chosen identifiers or verify services, so this is an error
    unless `custom.access_control.require_admin` is false. A warning is
    logged if groups are mapped, but `keep_group_subjects` is not set, as
    roles on services granted to groups then don't apply.

    Raises:
        RuntimeError: No group is mapped to the role `admin`, and
            `custom.access_control.require_admin` is true.
    """
    conf = get_config().security.access_control
    mapping = conf.group_mapping or {}
    # FOCA drops group claim values that aren't mapped once a mapping is set,
    # also an empty one
    if conf.group_mapping is not None and not conf.keep_group_subjects:
        logger.warning(
            "`security.access_control.keep_group_subjects` is not set, so "
            "roles on services granted to groups don't apply"
        )
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
    # Requests to the API are routed through its blueprint, so it is set
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


def _get_current_claims() -> dict[str, Any]:
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
    """Store the current subject's display name for services with a role.

    Stored for the services the subject of the current request has a role on.
    Names are shown to owners and admins listing the roles on a service, so
    they are only stored per service, once the user has used the registry with
    a role on it (and are removed with the role). Nothing is stored if the
    identity provider reports no name.

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
    """Delete display names stored for users on a service.

    See `remember_current_user()`.

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
    # Operations that change roles require authentication, so the request
    # has a subject
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
    """Revoke the owner role of the current subject on a service.

    Also deletes the name stored for the subject of the current request on the
    service (e.g., if the service they registered was deleted meanwhile). Other
    roles on the service (e.g., of the owner of a service registered again with
    the identifier) are kept.

    Args:
        serviceId: Service identifier.
    """
    # Operations that change roles require authentication, so the request
    # has a subject
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
def getServicePermissions(serviceId: str, **kwargs: Any) -> list[dict[str, Any]]:
    """List roles granted on a service.

    Args:
        serviceId: Service identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Roles granted on the service: user identifier, display name (if
        known) and role of each role granted to a user, followed by group
        and role of each role granted to a group.
    """
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    names = {
        user["user_id"]: user["name"]
        for user in get_collection(USER_NAMES).find({"service_id": serviceId})
    }
    users = [
        {"user_id": user_id, "role": role}
        | ({"name": names[user_id]} if user_id in names else {})
        for user_id, role in list_resource_roles(resource, subject_type="user")
    ]
    groups = [
        {"group": group, "role": role}
        for group, role in list_resource_roles(resource, subject_type="group")
    ]
    return users + groups


def _describe(subject: str, subject_type: SubjectType) -> str:
    """Describe a subject for log messages.

    Args:
        subject: User identifier or group.
        subject_type: Type of the subject.

    Returns:
        Quoted subject, with non-printable characters escaped; groups are
        marked as such (e.g., `group 'abc'`).
    """
    quoted = f"'{escape_log_value(subject)}'"
    return quoted if subject_type == "user" else f"group {quoted}"


def _roles_of(resource: str, subject: str, subject_type: SubjectType) -> list[str]:
    """Get the roles of a subject on a service.

    Args:
        resource: Resource path of the service.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Returns:
        Roles of the subject.
    """
    return [
        role
        for other, role in list_resource_roles(resource, subject_type=subject_type)
        if other == subject
    ]


def _owners(resource: str) -> list[tuple[SubjectType, str]]:
    """Get the owners of a service: users and groups.

    Args:
        resource: Resource path of the service.

    Returns:
        Type and identifier (user identifier or group) of each owner.
    """
    return [
        (subject_type, subject)
        for subject_type in SUBJECT_TYPES
        for subject, role in list_resource_roles(resource, subject_type=subject_type)
        if role == "owner"
    ]


def _check_other_owners(
    resource: str, subject: str, subject_type: SubjectType = "user"
) -> None:
    """Check that a service has owners (users or groups) other than a subject.

    Args:
        resource: Resource path of the service.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.Conflict: The subject is the only owner of
            the service.
    """
    if _owners(resource) == [(subject_type, subject)]:
        logger.info(
            "%s is the last owner of '%s'",
            _describe(subject, subject_type),
            escape_log_value(resource),
        )
        raise Conflict


def _grant(
    serviceId: str,
    resource: str,
    subject: str,
    role: str,
    subject_type: SubjectType = "user",
) -> None:
    """Grant a user or group a role on a service, unless it was deleted.

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
        subject: User identifier or group.
        role: Role to grant.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.NotFound: The service was deleted
            meanwhile.
        ValueError: The subject can't be stored in policy rules.
    """
    add_resource_role(
        subject=subject, role=role, resource=resource, subject_type=subject_type
    )
    if not service_exists(serviceId):
        remove_resource_role(
            subject=subject, role=role, resource=resource, subject_type=subject_type
        )
        if subject_type == "user":
            _forget_names(serviceId, subject)
        raise NotFound


def _restore_owner_if_none(
    serviceId: str, resource: str, subject: str, subject_type: SubjectType = "user"
) -> None:
    """Make a former owner (user or group) owner again if none are left.

    Checking for other owners before changing roles is not atomic:
    concurrent requests of owners removing each other could leave a service
    without owners. Each request therefore checks again afterwards and, if
    no owner is left, restores the owner role it removed.

    Args:
        serviceId: Service identifier.
        resource: Resource path of the service.
        subject: User identifier or group whose owner role was removed.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.Conflict: The service had no owners left.
        cloud_registry.exceptions.NotFound: The service was deleted
            meanwhile.
    """
    if not _owners(resource):
        # A concurrent deletion of the service removed all roles, unless
        # the service still exists afterwards
        _grant(serviceId, resource, subject, "owner", subject_type)
        logger.info(
            "Restored owner %s of '%s', removed concurrently by '%s'",
            _describe(subject, subject_type),
            escape_log_value(resource),
            current_user(),
        )
        raise Conflict


def _put_role(serviceId: str, subject: str, subject_type: SubjectType) -> None:
    """Grant a user or group the role in the request body, replacing others.

    Other roles on the service are revoked before the role is granted, so
    that a failure leaves the subject with fewer rights, not more.
    On a user's first role on the service, a name stored for them on it is
    deleted (cf. `remember_current_user()`).

    Args:
        serviceId: Service identifier.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.BadRequest: The subject can't be stored in
            policy rules.
        cloud_registry.exceptions.Conflict: The subject is the last owner of
            the service, and the role is not `owner`.
        cloud_registry.exceptions.NotFound: The service doesn't exist, or
            was deleted meanwhile (cf. `_grant()`).
    """
    role = request.json["role"]
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    try:
        previous_roles = _roles_of(resource, subject, subject_type)
        if role != "owner":
            _check_other_owners(resource, subject, subject_type)
        for other_role in ROLES:
            if other_role != role:
                remove_resource_role(
                    subject=subject,
                    role=other_role,
                    resource=resource,
                    subject_type=subject_type,
                )
        if "owner" in previous_roles and role != "owner":
            _restore_owner_if_none(serviceId, resource, subject, subject_type)
        if not previous_roles and subject_type == "user":
            _forget_names(serviceId, subject)
        _grant(serviceId, resource, subject, role, subject_type)
    # FOCA rejects values that can't be stored in policy rules (e.g.,
    # subjects with commas)
    except ValueError as exc:
        logger.info("Invalid permission: %s", escape_log_value(exc))
        raise BadRequest from exc
    logger.info(
        "Role '%s' on service '%s' granted to %s by '%s' (previous roles: %s).",
        role,
        escape_log_value(serviceId),
        _describe(subject, subject_type),
        current_user(),
        ", ".join(previous_roles) or "none",
    )


def _delete_roles(serviceId: str, subject: str, subject_type: SubjectType) -> None:
    """Revoke all roles of a user or group on a service.

    Args:
        serviceId: Service identifier.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.Conflict: The subject is the last owner of
            the service.
        cloud_registry.exceptions.NotFound: The service doesn't exist, or
            the subject has no role on it.
    """
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    _check_other_owners(resource, subject, subject_type)
    was_owner = (subject_type, subject) in _owners(resource)
    revoked = False
    for role in ROLES:
        try:
            revoked |= remove_resource_role(
                subject=subject, role=role, resource=resource, subject_type=subject_type
            )
        # FOCA rejects values that can't be in policy rules, so no role
        # exists for them
        except ValueError as exc:
            raise NotFound from exc
    if not revoked:
        raise NotFound
    if was_owner:
        _restore_owner_if_none(serviceId, resource, subject, subject_type)
    if subject_type == "user":
        _forget_names(serviceId, subject)
    logger.info(
        "Roles on service '%s' of %s revoked by '%s'.",
        escape_log_value(serviceId),
        _describe(subject, subject_type),
        current_user(),
    )


def _check_user_id(userId: str) -> None:
    """Check that a user identifier is not `groups`.

    `/services/{serviceId}/permissions/groups` is the path of the roles of
    groups without a group (cf. `putServiceGroupPermission()`), not of a
    user `groups`, so it is not found, like `.../permissions/groups/`.

    Args:
        userId: User identifier.

    Raises:
        cloud_registry.exceptions.NotFound: The user identifier is `groups`.
    """
    if userId == "groups":
        raise NotFound


# PUT /services/{serviceId}/permissions/{userId}
def putServicePermission(serviceId: str, userId: str, **kwargs: Any) -> dict[str, Any]:
    """Grant a user a role on a service, replacing any other role.

    See `_put_role()`.

    Args:
        serviceId: Service identifier.
        userId: User identifier (`sub` claim of the user's tokens).
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        User identifier and granted role.

    Raises:
        cloud_registry.exceptions.NotFound: The user identifier is `groups`
            (cf. `_check_user_id()`).
    """
    _check_user_id(userId)
    _put_role(serviceId, userId, "user")
    return {"user_id": userId, "role": request.json["role"]}


# DELETE /services/{serviceId}/permissions/{userId}
def deleteServicePermission(serviceId: str, userId: str, **kwargs: Any) -> str:
    """Revoke all roles of a user on a service (cf. `_delete_roles()`).

    Args:
        serviceId: Service identifier.
        userId: User identifier.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        User identifier.

    Raises:
        cloud_registry.exceptions.NotFound: The user identifier is `groups`
            (cf. `_check_user_id()`).
    """
    _check_user_id(userId)
    _delete_roles(serviceId, userId, "user")
    return userId


# PUT /services/{serviceId}/permissions/groups/{entitlement}
def putServiceGroupPermission(
    serviceId: str, entitlement: str, **kwargs: Any
) -> dict[str, Any]:
    """Grant a group a role on a service, replacing any other role.

    See `_put_role()`. Owners may only grant roles to groups they are members
    of (i.e., whose entitlement is in their group claim), as they could
    otherwise hand a service to any group; admins may grant roles to any group.

    Args:
        serviceId: Service identifier.
        entitlement: Group, i.e., a value of the group claim of its members.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Group and granted role.

    Raises:
        cloud_registry.exceptions.Forbidden: The subject of the current
            request is neither a member of the group nor an admin.
    """
    if entitlement not in get_current_group_values() and not is_admin():
        logger.info(
            "Role on service '%s' not granted to group '%s' by '%s': not a "
            "member of the group",
            escape_log_value(serviceId),
            escape_log_value(entitlement),
            current_user(),
        )
        raise Forbidden
    _put_role(serviceId, entitlement, "group")
    return {"group": entitlement, "role": request.json["role"]}


# DELETE /services/{serviceId}/permissions/groups/{entitlement}
def deleteServiceGroupPermission(
    serviceId: str, entitlement: str, **kwargs: Any
) -> str:
    """Revoke all roles of a group on a service (cf. `_delete_roles()`).

    Any owner may revoke roles of groups, also of groups they are not a
    member of.

    Args:
        serviceId: Service identifier.
        entitlement: Group.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The group.
    """
    _delete_roles(serviceId, entitlement, "group")
    return entitlement


# GET /me
def getMe(**kwargs: Any) -> dict[str, Any]:
    """Get the current user, and store or refresh their display name.

    The name is stored for the services the user has a role on.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        User identifier, whether the user is an admin (cf. `is_admin()`),
        and the user's roles on services: granted to the user, and granted
        to groups the user is a member of (with `group`), sorted by service
        identifier (roles of the user first). Names are only stored for
        services with roles granted to the user, as only these are listed
        with names (cf. `getServicePermissions()`).
    """
    # The endpoint requires authentication, so the request has a subject
    user_id: str = get_current_subject()  # type: ignore[assignment]
    prefix = get_config().security.access_control.subject_prefix
    services_path = f"{_api_base_path()}/services/"
    enforcer = get_enforcer()
    # Group subjects as enforced (cf. `keep_group_subjects`), without the
    # roles that groups are mapped to
    groups = [group for group in get_current_groups() if group.startswith(GROUP_PREFIX)]
    services = []
    for subject in [f"{prefix}{user_id}", *groups]:
        for _, role, resource in enforcer.get_filtered_named_grouping_policy(
            RESOURCE_ROLE_PTYPE, 0, subject
        ):
            service_id = resource.removeprefix(services_path)
            if resource.startswith(services_path) and "/" not in service_id:
                services.append(
                    {"service_id": service_id, "role": role}
                    | (
                        {"group": subject.removeprefix(GROUP_PREFIX)}
                        if subject in groups
                        else {}
                    )
                )
    remember_current_user(
        [service["service_id"] for service in services if "group" not in service]
    )
    return {
        "user_id": user_id,
        "admin": is_admin(),
        "services": sorted(
            services,
            key=lambda service: (service["service_id"], service.get("group", "")),
        ),
    }
