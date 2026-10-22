"""Access control: policies, service ownership and permission controllers.

The controllers manage permissions on services. Roles
(`security.access_control.resource_roles`) are granted to users (by their
identifier) or to groups (values of the group claim,
`security.access_control.claim_groups`); groups count like users, e.g., a group
may be the only owner of a service. Roles are granted, revoked and listed with
FOCA's resource ownership helpers, which keep the last owner of a service
(`409 Conflict`) and log changes of roles (who changed which role of whom on
which service), for auditing. The app adds the display names of users, the
check that services exist, and which groups users may grant roles to.

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
    get_current_group_values,
    get_current_subject,
    grant_role,
    is_admin,
    list_roles,
    list_subject_roles,
    remove_resource,
    remove_resource_role,
    revoke_roles,
)
from foca.security.access_control import add_owner as add_resource_owner
from foca.utils.logging import escape_log_value

from cloud_registry.exceptions import (
    BadRequest,
    Forbidden,
    NotFound,
    Unauthorized,
)
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)

# Collection with the display names of users with roles on services
USER_NAMES = "user_names"


def check_admin_groups() -> None:
    """Check that groups are mapped to the role `admin`, and log them.

    Without such groups, nobody can manage services they don't own, register
    services with chosen identifiers or verify services, so this is an error
    unless `custom.access_control.require_admin` is false. A warning is
    logged if groups are mapped, but `keep_group_subjects` is not set, as
    roles on services granted to groups don't apply then (and FOCA refuses
    to grant them).

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
            "roles on services granted to groups don't apply, and can't be "
            "granted (`400 Bad Request`)"
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


def check_no_service_clients() -> None:
    """Check that service clients are not configured, as they aren't supported.

    With service clients (`security.auth.service_clients`), FOCA's ownership
    helpers attribute new services to the client, or to the user it names
    (`security.auth.on_behalf_of_header`), while the app's endpoints only
    list and manage roles of users and groups, and take the token's subject
    as the user.

    Raises:
        RuntimeError: Service clients or the header naming the user they act
            for are configured.
    """
    auth = get_config().security.auth
    if auth.service_clients or auth.on_behalf_of_header is not None:
        raise RuntimeError(
            "Service clients are not supported: remove "
            "`security.auth.service_clients` and "
            "`security.auth.on_behalf_of_header`"
        )


def _api_base_path() -> str:
    """Get the base path of the API handling the current request.

    Returns:
        Base path, e.g., `/ga4gh/registry/v1` (Connexion registers each API
        as a Flask blueprint with its base path as URL prefix).

    Raises:
        RuntimeError: The current request is not routed through a blueprint,
            i.e., not handled by an API.
    """
    if request.blueprint is None:
        raise RuntimeError("The current request is not handled by an API")
    return current_app.blueprints[request.blueprint].url_prefix or ""


def _require_subject() -> str:
    """Get the subject of the current request, which must be authenticated.

    For operations that require authentication, so that their requests have
    a subject.

    Returns:
        User identifier.

    Raises:
        Unauthorized: The current request has no subject.
    """
    subject = get_current_subject()
    if subject is None:
        raise Unauthorized
    return subject


def services_path() -> str:
    """Get the path of the services, under which roles are granted.

    Returns:
        Request path of the service collection, including the base path of
        the API handling the current request.
    """
    return f"{_api_base_path()}/services"


def service_path(serviceId: str) -> str:
    """Get resource path of a service, i.e., the path roles are granted on.

    Args:
        serviceId: Service identifier.

    Returns:
        Request path of the service, including the base path of the API
        handling the current request.
    """
    return f"{services_path()}/{serviceId}"


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
    """Make the subject of the current request owner of a new service.

    See FOCA's `add_owner()`, which logs the new role.

    Args:
        serviceId: Service identifier.

    Raises:
        Unauthorized: The current request has no subject.
        ValueError: The role can't be stored (e.g., for service identifiers
            with commas).
    """
    _require_subject()
    add_resource_owner(service_path(serviceId))
    remember_current_user([serviceId])


def remove_owner(serviceId: str) -> None:
    """Revoke the owner role of the current subject on a service.

    Also deletes the name stored for the subject of the current request on the
    service (e.g., if the service they registered was deleted meanwhile). Other
    roles on the service (e.g., of the owner of a service registered again with
    the identifier) are kept.

    Args:
        serviceId: Service identifier.

    Raises:
        Unauthorized: The current request has no subject.
    """
    user_id = _require_subject()
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
    names = {
        user["user_id"]: user["name"]
        for user in get_collection(USER_NAMES).find({"service_id": serviceId})
    }
    roles = list_roles(service_path(serviceId))
    users = [
        {"user_id": role.subject, "role": role.role}
        | ({"name": names[role.subject]} if role.subject in names else {})
        for role in roles
        if role.subject_type == "user"
    ]
    groups = [
        {"group": role.subject, "role": role.role}
        for role in roles
        if role.subject_type == "group"
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


def _put_role(serviceId: str, subject: str, subject_type: SubjectType) -> None:
    """Grant a user or group the role in the request body, replacing others.

    See FOCA's `grant_role()`, which keeps the last owner, revokes the role
    again if the service was deleted meanwhile, and logs the change. On a
    user's first role on the service, a name stored for them on it is deleted
    (cf. `remember_current_user()`), as it is if the service was deleted
    meanwhile.

    Args:
        serviceId: Service identifier.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Raises:
        cloud_registry.exceptions.BadRequest: The role can't be granted to
            the subject (cf. `grant_role()`), e.g., as it can't be stored in
            policy rules.
        foca.security.access_control.LastOwner: The subject is the last owner
            of the service, and the role is not `owner` (a werkzeug
            `Conflict`, so `409`, cf. `cloud_registry.exceptions`).
        cloud_registry.exceptions.NotFound: The service doesn't exist, or
            was deleted meanwhile.
    """
    role = request.json["role"]
    if not service_exists(serviceId):
        raise NotFound
    resource = service_path(serviceId)
    is_user = subject_type == "user"
    if is_user and all(
        (granted.subject_type, granted.subject) != ("user", subject)
        for granted in list_roles(resource)
    ):
        _forget_names(serviceId, subject)
    try:
        grant_role(
            resource,
            subject,
            role,
            subject_type=subject_type,
            exists=lambda: service_exists(serviceId),
        )
    # FOCA rejects values that can't be stored in policy rules (e.g.,
    # subjects with commas), and roles that wouldn't apply
    except ValueError as exc:
        logger.info(
            "Invalid permission of %s on service '%s': %s",
            _describe(subject, subject_type),
            escape_log_value(serviceId),
            escape_log_value(exc),
        )
        raise BadRequest from exc
    except NotFound:
        if is_user:
            _forget_names(serviceId, subject)
        raise


def _delete_roles(serviceId: str, subject: str, subject_type: SubjectType) -> None:
    """Revoke all roles of a user or group on a service.

    See FOCA's `revoke_roles()`, which keeps the last owner and logs the
    change.

    Args:
        serviceId: Service identifier.
        subject: User identifier or group.
        subject_type: Type of the subject.

    Raises:
        foca.security.access_control.LastOwner: The subject is the last owner
            of the service (a werkzeug `Conflict`, so `409`).
        cloud_registry.exceptions.NotFound: The service doesn't exist, or
            the subject has no role on it.
    """
    if not service_exists(serviceId):
        raise NotFound
    try:
        revoked = revoke_roles(
            service_path(serviceId),
            subject,
            subject_type=subject_type,
            exists=lambda: service_exists(serviceId),
        )
    # FOCA rejects resources that can't be in policy rules (e.g., of services
    # stored with commas in their identifiers), so no role exists on them
    except ValueError as exc:
        raise NotFound from exc
    if not revoked:
        raise NotFound
    if subject_type == "user":
        _forget_names(serviceId, subject)


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
            `token_info`, and the request body); unused.

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
            `token_info`, and the request body); unused.

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
        User identifier, whether the user is an admin (cf. FOCA's
        `is_admin()`), and the user's roles on services (cf. FOCA's
        `list_subject_roles()`): granted to the user, and granted to groups
        the user is a member of (with `group`), sorted by service identifier
        (roles of the user first). Names are only stored for services with
        roles granted to the user, as only these are listed with names (cf.
        `getServicePermissions()`).

    Raises:
        Unauthorized: The request has no subject (the endpoint requires
            authentication, so it always has one).
    """
    user_id = _require_subject()
    services = [
        {"service_id": role.id, "role": role.role}
        | ({"group": role.subject} if role.subject_type == "group" else {})
        for role in list_subject_roles(services_path())
    ]
    remember_current_user(
        [service["service_id"] for service in services if "group" not in service]
    )
    return {"user_id": user_id, "admin": is_admin(), "services": services}
