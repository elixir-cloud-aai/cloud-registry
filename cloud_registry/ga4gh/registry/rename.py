"""Controller for renaming services, i.e., changing their identifiers.

Only admins may rename services: no policy in `config.yaml` allows the
operation to other users. Everything stored under the old identifier moves to
the new one: the service document (with its verification and `createdAt`),
the roles on the service and the names of its users.

MongoDB transactions need a replica set, which deployments (Docker Compose,
Helm chart) don't run, so renames are ordered to be safe without them:

1. The old and the new identifier are locked (one document each in
   `RENAMES`, by identifier), so that no other rename uses either until this
   one is done (`409`).
2. Roles left on the new identifier (e.g., by a deleted service whose roles
   couldn't be revoked) are revoked, as when a service is registered.
3. The service document gets the new identifier, in a single write: the
   service is never registered under both identifiers, and its old one
   returns `404` from then on. The unique index of identifiers rejects new
   identifiers registered meanwhile (`409`); if the service was deleted
   meanwhile, nothing moves (`404`). Up to here, failures release the locks
   and leave the service as it was.
4. The roles and names are copied to the new identifier, then removed from
   the old one, and the locks released. Roles changed on the old identifier
   before step 3 are copied; after step 3, FOCA's helpers reject changes of
   roles on it, as the service doesn't exist.

If step 4 fails (e.g., the app crashes, or the database is unavailable), the
service may lack roles under its new identifier until the rename is resumed
(cf. `resume_renames()`): on the next rename, and when the app starts, for
renames that started at least `RENAME_TIMEOUT` ago (so that running ones are
not resumed). Whether the service moved is told from which identifiers are
registered: it is finished if only the new one is, rolled back if the old one
still is.
"""

from datetime import datetime, timedelta
import logging
from typing import Any

from foca.security.access_control import (
    add_resource_role,
    list_roles,
    remove_resource,
)
from foca.security.access_control.foca_casbin_adapter.casbin_rule import (
    validate_rule_value,
)
from foca.utils.logging import escape_log_value
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import CodedConflict, NotFound, problem
from cloud_registry.ga4gh.registry.permissions import (
    USER_NAMES,
    current_user,
    remove_permissions,
    services_path,
)
from cloud_registry.ga4gh.registry.service import (
    UPDATED,
    InvalidService,
    check_strings,
    service_exists,
)
from cloud_registry.utils import format_time, get_collection, now

logger = logging.getLogger(__name__)
# Collection with the locks of renames: per rename, a document for the old
# identifier (`_id`, with the new one as `to`) and one for the new identifier
# (`_id`, with the old one as `from`); both with the time the rename started
# (`started_at`), and the old one with the path of the services (`path`)
RENAMES = "service_renames"
# Time after which renames that haven't finished are resumed; renames take a
# few writes, so they are done by then unless they failed
RENAME_TIMEOUT = timedelta(minutes=1)


class ServiceIdTaken(CodedConflict):
    """A service with the new identifier is registered."""

    problem_code = "service-id-taken"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "A service with the new identifier is registered; choose another one."
        )


class RenameInProgress(CodedConflict):
    """The service or the new identifier is being renamed."""

    problem_code = "rename-in-progress"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "The service or the new identifier is being renamed; try again later."
        )


# POST /services/{serviceId}/rename
def renameService(serviceId: str, rename: dict[str, Any], **kwargs: Any) -> Any:
    """Change the identifier of a service (cf. the module docstring).

    Interrupted renames are resumed first (cf. `resume_renames()`).

    Args:
        serviceId: Identifier of the service.
        rename: Request body, with the new identifier as `id`.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        New identifier; or an error response: `400` for identifiers that
        can't be used in URLs or roles, `409` if a service with the new
        identifier is registered, or if the service or the new identifier is
        being renamed.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, or was
            deleted meanwhile.
    """
    new_id: str = rename["id"]
    resume_renames()
    try:
        _check_id(new_id)
    except InvalidService as exc:
        logger.info(
            "Service '%s' not renamed: %s",
            escape_log_value(serviceId),
            escape_log_value(exc.description),
        )
        return problem(exc)
    if not service_exists(serviceId):
        raise NotFound
    if service_exists(new_id):
        return problem(ServiceIdTaken())
    path = services_path()
    if not _lock(path, serviceId, new_id):
        return problem(RenameInProgress())
    try:
        # As when a service is registered (cf. `_register_service()`); the
        # new identifier is not registered (checked above), unless an admin
        # registers it concurrently, which then fails or loses its roles
        remove_permissions(new_id)
        moved = _move_service(serviceId, new_id)
    except DuplicateKeyError:
        _unlock(serviceId, new_id)
        return problem(ServiceIdTaken())
    except Exception:
        _unlock(serviceId, new_id)
        raise
    if not moved:
        _unlock(serviceId, new_id)
        raise NotFound
    # failures leave the locks, so that the rename is resumed
    _move_roles(path, serviceId, new_id)
    _unlock(serviceId, new_id)
    logger.info(
        "Service '%s' renamed to '%s' by '%s'.",
        escape_log_value(serviceId),
        escape_log_value(new_id),
        current_user(),
    )
    return new_id


def _check_id(new_id: str) -> None:
    """Check that a new identifier can be used.

    The API specification rejects identifiers that can't be used in URLs, as
    for `PUT /services/{serviceId}` (dot segments, `types`, slashes; cf.
    `cloud_registry.ga4gh.registry.service.RESERVED_IDS`).

    Args:
        new_id: New identifier of a service.

    Raises:
        InvalidService: The identifier has unpaired surrogates, or can't be
            stored in roles (e.g., with commas or line breaks).
    """
    check_strings(new_id, field="id")
    try:
        validate_rule_value(f"{services_path()}/{new_id}")
    except ValueError as exc:
        raise InvalidService(
            f"The new identifier can't be used in roles: {exc}."
        ) from exc


def _lock(path: str, source: str, target: str) -> bool:
    """Lock the identifiers of a rename.

    Args:
        path: Path of the services, under which roles are granted (cf.
            `services_path()`).
        source: Old identifier.
        target: New identifier.

    Returns:
        Whether both were locked; `False` if either is locked already (then
        neither is locked by this call).
    """
    collection = get_collection(RENAMES)
    started_at = now()
    try:
        collection.insert_one(
            {"_id": source, "to": target, "path": path, "started_at": started_at}
        )
    except DuplicateKeyError:
        return False
    try:
        collection.insert_one({"_id": target, "from": source, "started_at": started_at})
    except DuplicateKeyError:
        collection.delete_one({"_id": source, "to": target})
        return False
    return True


def _unlock(source: str, target: str) -> None:
    """Release the locks of a rename (cf. `_lock()`).

    Args:
        source: Old identifier.
        target: New identifier.
    """
    collection = get_collection(RENAMES)
    collection.delete_one({"_id": target, "from": source})
    collection.delete_one({"_id": source, "to": target})


def _move_service(source: str, target: str) -> bool:
    """Change the identifier of a stored service, renewing `updatedAt`.

    Args:
        source: Old identifier.
        target: New identifier.

    Returns:
        Whether the service was changed; `False` if it doesn't exist.

    Raises:
        pymongo.errors.DuplicateKeyError: A service with the new identifier
            is registered.
    """
    result = get_collection("services").update_one(
        {"id": source}, {"$set": {"id": target, UPDATED: format_time(now())}}
    )
    return result.matched_count > 0


def _move_roles(path: str, source: str, target: str) -> None:
    """Move the roles and names of users on a service to a new identifier.

    Copied first, then removed from the old identifier, so that the steps can
    be repeated if they fail. Roles that can't be granted (e.g., stored
    before FOCA rejected such values) are not copied, with a warning.

    Args:
        path: Path of the services, under which roles are granted.
        source: Old identifier.
        target: New identifier.
    """
    resource = f"{path}/{source}"
    for role in list_roles(resource):
        try:
            add_resource_role(
                role.subject,
                role.role,
                f"{path}/{target}",
                subject_type=role.subject_type,
            )
        except ValueError as exc:
            logger.warning(
                "Role '%s' of %s '%s' on service '%s' not moved to '%s': %s",
                escape_log_value(role.role),
                role.subject_type,
                escape_log_value(role.subject),
                escape_log_value(source),
                escape_log_value(target),
                escape_log_value(exc),
            )
    names = get_collection(USER_NAMES)
    for name in names.find({"service_id": source}):
        names.update_one(
            {"service_id": target, "user_id": name["user_id"]},
            {"$setOnInsert": {"name": name["name"]}},
            upsert=True,
        )
    remove_resource(resource)
    names.delete_many({"service_id": source})


def resume_renames(min_age: timedelta = RENAME_TIMEOUT) -> None:
    """Finish or roll back renames that were interrupted.

    For renames that started at least `min_age` ago (cf. the module
    docstring): renames of services that are only registered under their
    new identifier are finished (cf. `_move_roles()`); renames of services
    that are still registered under their old identifier are rolled back,
    i.e., their locks released (nothing else has changed then); if both
    identifiers are registered (e.g., one was registered again meanwhile),
    too, with a warning, as roles might otherwise move to another service; if
    neither is, the roles left on the old identifier are removed. Each rename
    is resumed once, also by concurrent calls (cf. `_claim()`). Locks of new
    identifiers whose rename has no lock are removed.

    Must be called in an app context.

    Args:
        min_age: How long ago renames must have started to be resumed.
    """
    collection = get_collection(RENAMES)
    cutoff = now() - min_age
    stale = {"started_at": {"$lte": cutoff}}
    for lock in collection.find({"to": {"$exists": True}, **stale}):
        if _claim(lock):
            _finish(lock["path"], lock["_id"], lock["to"])
    for lock in collection.find({"from": {"$exists": True}, **stale}):
        if not collection.count_documents(
            {"_id": lock["from"], "to": lock["_id"]}, limit=1
        ):
            collection.delete_one({"_id": lock["_id"], "from": lock["from"]})


def _claim(lock: dict[str, Any]) -> bool:
    """Claim an interrupted rename, so that only one call resumes it.

    Its start is set to the current time, unless it changed since the lock
    was read (i.e., another call claimed it).

    Args:
        lock: Lock of the old identifier, as read.

    Returns:
        Whether the rename was claimed.
    """
    started_at: datetime = lock["started_at"]
    claimed = get_collection(RENAMES).find_one_and_update(
        {"_id": lock["_id"], "started_at": started_at},
        {"$set": {"started_at": now()}},
    )
    return claimed is not None


def _finish(path: str, source: str, target: str) -> None:
    """Finish or roll back an interrupted rename (cf. `resume_renames()`).

    Args:
        path: Path of the services, under which roles are granted.
        source: Old identifier.
        target: New identifier.
    """
    names = (escape_log_value(source), escape_log_value(target))
    source_exists, target_exists = service_exists(source), service_exists(target)
    if source_exists and target_exists:
        logger.warning(
            "Interrupted rename of service '%s' to '%s' rolled back, as both "
            "are registered; check the roles of both.",
            *names,
        )
    elif source_exists:
        logger.info("Interrupted rename of service '%s' to '%s' rolled back.", *names)
    elif target_exists:
        _move_roles(path, source, target)
        logger.info("Interrupted rename of service '%s' to '%s' finished.", *names)
    else:
        remove_resource(f"{path}/{source}")
        get_collection(USER_NAMES).delete_many({"service_id": source})
        logger.info(
            "Interrupted rename of service '%s' to '%s' ended: neither is "
            "registered; roles on '%s' removed.",
            *names,
            names[0],
        )
    _unlock(source, target)
