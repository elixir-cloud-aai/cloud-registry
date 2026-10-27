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

from collections.abc import Callable
from functools import wraps
import logging
from typing import Any
from urllib.parse import quote

from flask import current_app, g, has_request_context, request
from flask.globals import request_ctx
from foca import get_config, get_custom_config
from foca.security.access_control import (
    SubjectType,
    get_available_opt_in_roles,
    get_current_group_values,
    get_current_subject,
    grant_role,
    is_admin,
    is_permitted,
    list_roles,
    list_subject_roles,
    remove_resource,
    remove_resource_role,
    revoke_roles,
)
from foca.security.access_control import add_owner as add_resource_owner
from foca.security.access_control.constants import ADMIN_ROLE
from foca.security.access_control.enforcement import _enforces_access_control
from foca.security.auth import validate_token
from foca.utils.logging import escape_log_value

from cloud_registry.exceptions import (
    BadRequest,
    Forbidden,
    NotFound,
    Unauthorized,
)
from cloud_registry.ga4gh.registry.locks import changing
from cloud_registry.ga4gh.registry.service import service_exists
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)


def _bearer_token() -> str | None:
    """Get the bearer token sent with the current request.

    Returns:
        The token in the `Authorization` header if its scheme is `Bearer`
        (in any case, cf. RFC 6750; empty if it has none), or `None`. Split
        at any whitespace, as Connexion does.
    """
    parts = request.headers.get("Authorization", "").split(None, 1)
    if not parts or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() if len(parts) > 1 else ""


def _is_invalid_token(exc: Unauthorized) -> bool:
    """Check whether a rejection of a token is because it is invalid.

    Args:
        exc: Exception of FOCA's `validate_token()`.

    Returns:
        Whether its challenge has the error `invalid_token` (RFC 6750); FOCA
        leaves it out if the token couldn't be validated for other reasons
        (e.g., the identity provider is unavailable).
    """
    challenge = getattr(exc, "www_authenticate", None) or []
    return any(item.parameters.get("error") == "invalid_token" for item in challenge)


# Attribute of `flask.g` with the rejection of the token of the request
_TOKEN_ERROR = "cloud_registry_token_error"  # noqa: S105 (attribute name)


def validate_bearer_token(token: str) -> dict[str, Any]:
    """Validate a bearer token, keeping why it was rejected.

    The function that Connexion validates tokens with (`x-bearerInfoFunc`
    in `config.yaml`): FOCA's `validate_token()`, but its exception is also
    kept for the request, so that `check_read_token()` can tell why a token
    of a public read was rejected without validating it again.

    Args:
        token: Bearer token.

    Returns:
        Token info (cf. FOCA's `validate_token()`).

    Raises:
        connexion.exceptions.Unauthorized: The token was rejected.
    """
    try:
        return validate_token(token)
    except Unauthorized as exc:
        setattr(g, _TOKEN_ERROR, exc)
        raise


def check_read_token[T](func: Callable[..., T]) -> Callable[..., T]:
    """Reject invalid tokens sent with reads that don't require login.

    Public reads accept requests without a token (anonymous) and with one
    (for the visibility rules of the user; cf. `security` in the API
    specification). Connexion treats requests whose token it can't validate
    as anonymous then; so that clients notice (e.g., an expired token) and
    log in again, rather than see less without a hint, requests with an
    invalid token are rejected as on other operations (`401`, with an
    `invalid_token` challenge), with the exception kept when Connexion
    validated it (cf. `validate_bearer_token()`; validated again only if
    Connexion didn't, e.g., for an empty token). If it can't be validated
    for other reasons (e.g., the identity provider is unavailable), the
    request stays anonymous, so that public reads keep working, and this is
    logged.

    Args:
        func: Controller of a read.

    Returns:
        The controller, checking the token first.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        """Check the token sent with the request, then call the controller.

        Args:
            *args: Positional arguments of the controller.
            **kwargs: Keyword arguments of the controller.

        Returns:
            Response of the controller.
        """
        # requests not handled by Connexion (e.g., in unit tests) have none
        token = _bearer_token() if has_request_context() else None
        if token is not None:
            context = getattr(request_ctx, "connexion_context", None) or {}
            if not context.get("token_info"):
                try:
                    error = getattr(g, _TOKEN_ERROR, None)
                    if error is not None:
                        raise error
                    validate_token(token)
                except Unauthorized as exc:
                    if _is_invalid_token(exc):
                        raise
                    logger.info(
                        "Token sent with a public read not validated, read as "
                        "anonymous: %s",
                        escape_log_value(str(exc.description)),
                    )
        return func(*args, **kwargs)

    return wrapper


# Collection with the display names of users with roles on services
USER_NAMES = "user_names"


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


def may_read_service(serviceId: str) -> bool:
    """Check whether the current user may get a service (`GET`).

    The same decision as for a request to `GET /services/{serviceId}` with
    the token of the current request, and the opt-in roles it asks for
    (e.g., admin mode; `security.access_control.opt_in_header`): the route
    of that request decides whether FOCA enforces policies on it (only if
    reads require login, cf. "Requiring login for reads" in
    `docs/access-control.md`), and if so, its policies are evaluated by
    FOCA's `is_permitted()`, for the path and method of that request.

    The check relies on internals of FOCA (`_enforces_access_control()`) and
    Connexion (`connexion_context`), as FOCA has no public helper for the
    decision on another request; the tests pin it to that of `GET`.

    Args:
        serviceId: Service identifier.

    Returns:
        Whether the current user may get the service; `False` if the request
        isn't routed to `GET /services/{serviceId}` for the identifier (e.g.,
        for identifiers with slashes, which are routed to other operations or
        none).
    """
    connexion_context = getattr(request_ctx, "connexion_context", None)
    # a request context for the read, with the validated token of the
    # current request (Connexion's security handlers store it on the request
    # context); the identifier is encoded, as in requests, and routed decoded
    path = service_path(quote(serviceId, safe=""))
    # only the header of opt-in roles, so that the read is decided for the
    # same roles as the current request (e.g., in admin mode)
    header = get_config().security.access_control.opt_in_header
    opt_in = [(header, value) for value in request.headers.getlist(header)]
    with current_app.test_request_context(path, method="GET", headers=opt_in):
        rule = request.url_rule
        view = None if rule is None else current_app.view_functions.get(rule.endpoint)
        if view is None or request.view_args != {"serviceId": serviceId}:
            return False
        # as in FOCA's own check of routes (`ownership_server`), whose
        # helper is internal
        if not _enforces_access_control(view):
            return True
        # an attribute that Connexion adds
        request_ctx.connexion_context = connexion_context  # type: ignore[attr-defined]
        return is_permitted()


def may_add_service_types() -> bool:
    """Check whether the current user may add service types.

    Must be called in a request context.

    Returns:
        Whether the user is an admin, or the deployment lets any
        authenticated user add types (`custom.access_control.service_types`
        is `open` or `moderated`; cf.
        `cloud_registry.ga4gh.registry.service_types`).
    """
    mode = get_custom_config(CustomConfig).access_control.service_types
    return mode in ("open", "moderated") or is_admin()


def service_types_pending() -> bool:
    """Check whether service types that the current user adds are pending.

    Must be called in a request context.

    Returns:
        Whether the deployment moderates service types
        (`custom.access_control.service_types` is `moderated`) and the user
        is not an admin, so that their new types are pending until an admin
        approves them.
    """
    mode = get_custom_config(CustomConfig).access_control.service_types
    return mode == "moderated" and not is_admin()


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
    meanwhile. Not while the service is renamed (cf. `changing()`).

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
    with changing(serviceId):
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
    change. Not while the service is renamed (cf. `changing()`).

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
    with changing(serviceId):
        if not service_exists(serviceId):
            raise NotFound
        try:
            revoked = revoke_roles(
                service_path(serviceId),
                subject,
                subject_type=subject_type,
                exists=lambda: service_exists(serviceId),
            )
        # FOCA rejects resources that can't be in policy rules (e.g., of
        # services stored with commas in their identifiers), so no role
        # exists on them
        except ValueError as exc:
            raise NotFound from exc
        if not revoked:
            raise NotFound
        if subject_type == "user":
            _forget_names(serviceId, subject)


def _first_segment(path: str) -> str:
    """Get the first segment of a path, with backslashes as slashes.

    Args:
        path: Path, e.g., a user identifier.

    Returns:
        The part before the first slash or backslash (as in browsers).
    """
    return path.replace("\\", "/").split("/", 1)[0]


def check_namespaces() -> None:
    """Check that no trusted issuer has a namespace with the segment `groups`.

    Must be called in an app context. The identifiers of its users would
    start with `groups/`, the path of the roles of groups, so their roles
    couldn't be changed via the permissions API (cf. `_check_user_id()`).

    Raises:
        ValueError: The namespace of a trusted issuer
            (`security.auth.issuers`) is `groups` or starts with `groups/`.
    """
    for issuer, namespace in get_config().security.auth.get_namespaces().items():
        if namespace is not None and _first_segment(namespace) == "groups":
            raise ValueError(
                f"The namespace {namespace!r} of the issuer {issuer!r} must "
                "not start with 'groups/', the path of the roles of groups: "
                "set another namespace ('security.auth.issuers')"
            )


def _check_user_id(userId: str) -> None:
    """Check that a user identifier is not `groups`, nor starts with `groups/`.

    Backslashes count as slashes, as in browsers (cf. the patterns of
    `userId` in `cloud_registry/api/additions.openapi.yaml`).

    `/services/{serviceId}/permissions/groups` is the path of the roles of
    groups without a group (cf. `putServiceGroupPermission()`), not of a
    user `groups`, so it is not found, like `.../permissions/groups/`, which
    is routed here, as user identifiers may contain slashes (namespaced by
    issuer); paths with a group are routed to the roles of groups.

    Args:
        userId: User identifier.

    Raises:
        cloud_registry.exceptions.NotFound: The user identifier is `groups`
            or starts with `groups/`.
    """
    if _first_segment(userId) == "groups":
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


# PUT /services/{serviceId}/permissions/groups/{group}
def putServiceGroupPermission(
    serviceId: str, group: str, **kwargs: Any
) -> dict[str, Any]:
    """Grant a group a role on a service, replacing any other role.

    See `_put_role()`. Owners may only grant roles to groups they are members
    of (i.e., whose group claim contains it), as they could
    otherwise hand a service to any group; admins may grant roles to any group.

    Args:
        serviceId: Service identifier.
        group: Group, i.e., a value of the group claim of its members.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`, and the request body); unused.

    Returns:
        Group and granted role.

    Raises:
        cloud_registry.exceptions.Forbidden: The subject of the current
            request is neither a member of the group nor an admin.
    """
    if group not in get_current_group_values() and not is_admin():
        logger.info(
            "Role on service '%s' not granted to group '%s' by '%s': not a "
            "member of the group",
            escape_log_value(serviceId),
            escape_log_value(group),
            current_user(),
        )
        raise Forbidden
    _put_role(serviceId, group, "group")
    return {"group": group, "role": request.json["role"]}


# DELETE /services/{serviceId}/permissions/groups/{group}
def deleteServiceGroupPermission(serviceId: str, group: str, **kwargs: Any) -> str:
    """Revoke all roles of a group on a service (cf. `_delete_roles()`).

    Any owner may revoke roles of groups, also of groups they are not a
    member of.

    Args:
        serviceId: Service identifier.
        group: Group.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        The group.
    """
    _delete_roles(serviceId, group, "group")
    return group


# GET /me
def getMe(**kwargs: Any) -> dict[str, Any]:
    """Get the current user, and store or refresh their display name.

    The name is stored for the services the user has a role on.

    Args:
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        User identifier (`user_id`), whether the user is an admin on this
        request (`admin`; cf. FOCA's `is_admin()`; in admin mode, i.e., if
        the request asks for the opt-in role `admin`), whether they may
        switch to admin mode (`admin_available`; cf. FOCA's
        `get_available_opt_in_roles()`; always if `admin` is true, e.g.,
        without opt-in), whether they may add service types
        (`may_add_service_types`; cf. `may_add_service_types()`) and
        whether these are pending (`service_types_pending`; cf.
        `service_types_pending()`), and the user's roles on services (cf.
        FOCA's `list_subject_roles()`): granted to the user, and granted to
        groups the user is a member of (with `group`), sorted by service
        identifier (roles of the user first). Names are only stored for
        services with roles granted to the user, as only these are listed
        with names (cf. `getServicePermissions()`).

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
    admin = is_admin()
    return {
        "user_id": user_id,
        "admin": admin,
        # admins without opt-in (e.g., `opt_in_roles: []`) are always admins
        "admin_available": admin or ADMIN_ROLE in get_available_opt_in_roles(),
        "may_add_service_types": may_add_service_types(),
        "service_types_pending": service_types_pending(),
        "services": services,
    }
