"""Controller for renaming services, i.e., changing their identifiers.

Only admins may rename services: no policy in `config.yaml` allows the
operation to other users. Everything stored under the old identifier moves to
the new one: the service document (with its verification and `createdAt`),
the roles on the service and the names of its users.

MongoDB transactions need a replica set, which deployments (Docker Compose,
Helm chart) don't run, so renames are ordered to be safe without them:

1. Both identifiers are locked exclusively (cf.
   `cloud_registry.ga4gh.registry.locks`): no other rename uses either, and
   no other change of either service (e.g., of its roles) runs until the
   rename is done.
2. Roles left on the new identifier (e.g., by a deleted service whose roles
   couldn't be revoked) are revoked, as when a service is registered.
3. The service document gets the new identifier, in a single write: the
   service is never registered under both identifiers, and its old one
   returns `404` from then on. The unique index of identifiers rejects new
   identifiers registered meanwhile (e.g., by `POST /services`); up to here,
   failures release the locks and leave the service as it was.
4. The roles and names are copied to the new identifier, then removed from
   the old one, and the locks released.

If step 4 fails (e.g., the app crashes, or the database is unavailable), the
service may lack roles under its new identifier, and both identifiers stay
locked, until the rename is resumed (cf. `resume_renames()`), once its locks
are stale.
"""

from enum import Enum
import logging
import time
from typing import Any
from uuid import uuid4

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

from cloud_registry.exceptions import CodedConflict, NotFound, ServiceBusy, problem
from cloud_registry.ga4gh.registry.locks import (
    LOCKS,
    lock_exclusive,
    remove_stale_changes,
    stale_before,
    unlock,
)
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
# Minimum time between checks for interrupted renames per worker, on
# requests (cf. `resume_renames_periodically()`), in seconds
RESUME_INTERVAL = 10.0
# Time of the last check for interrupted renames in this worker (monotonic)
_last_resume: float | None = None


class ServiceIdTaken(CodedConflict):
    """A service with the new identifier is registered."""

    problem_code = "service-id-taken"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "A service with the new identifier is registered; choose another one."
        )


class _Moved(Enum):
    """Outcome of moving a service document (cf. `_start()`)."""

    MOVED = "moved"
    TAKEN = "taken"
    GONE = "gone"


# POST /services/{serviceId}/rename
def renameService(serviceId: str, rename: dict[str, Any], **kwargs: Any) -> Any:
    """Change the identifier of a service (cf. the module docstring).

    Errors with a description specific to the request are returned rather
    than raised, so that their problem details have it (cf.
    `cloud_registry.exceptions.problem()`).

    Args:
        serviceId: Identifier of the service.
        rename: Request body, with the new identifier as `id`.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        New identifier; or an error response: `400` for identifiers that
        can't be used in roles, `409` if a service with the new identifier is
        registered, or if either identifier is being renamed or changed.

    Raises:
        cloud_registry.exceptions.NotFound: The service doesn't exist, or was
            deleted meanwhile.
    """
    new_id: str = rename["id"]
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
    tokens = _lock(path, serviceId, new_id)
    if tokens is None:
        return problem(ServiceBusy())
    moved = _start(serviceId, new_id, tokens)
    if moved is _Moved.TAKEN:
        return problem(ServiceIdTaken())
    if moved is _Moved.GONE:
        raise NotFound
    # failures leave the locks, so that the rename is resumed
    _move_roles(path, serviceId, new_id)
    _unlock(serviceId, new_id, tokens)
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


def _lock(path: str, source: str, target: str) -> tuple[str, str] | None:
    """Lock both identifiers of a rename (cf. `lock_exclusive()`).

    Args:
        path: Path of the services, under which roles are granted (cf.
            `services_path()`), stored for resuming the rename.
        source: Old identifier.
        target: New identifier.

    Returns:
        Tokens of the locks of the old and new identifier, or `None` if
        either is locked or being changed (then neither is locked).
    """
    source_token = lock_exclusive(source, to=target, path=path)
    if source_token is None:
        return None
    target_token = lock_exclusive(target, **{"from": source})
    if target_token is None:
        unlock(source, source_token)
        return None
    return source_token, target_token


def _unlock(source: str, target: str, tokens: tuple[str, str]) -> None:
    """Release the locks of a rename (cf. `_lock()`).

    The lock of the new identifier first, so that no lock of a new
    identifier is left without the lock of its rename.

    Args:
        source: Old identifier.
        target: New identifier.
        tokens: Tokens of the locks of the old and new identifier.
    """
    unlock(target, tokens[1])
    unlock(source, tokens[0])


def _start(source: str, target: str, tokens: tuple[str, str]) -> _Moved:
    """Move the service document to the new identifier (steps 2 and 3).

    Checks the identifiers again, as they may have changed before they were
    locked. Unless the document moved, the locks are released, also on
    errors.

    Args:
        source: Old identifier.
        target: New identifier.
        tokens: Tokens of the locks (cf. `_lock()`).

    Returns:
        Whether the service moved, the new identifier was registered
        meanwhile, or the service was deleted meanwhile.
    """
    outcome = _Moved.GONE
    try:
        # checked again now that both identifiers are locked: the service may
        # have moved (e.g., by a repeated request), or the new identifier been
        # registered, since the first checks; its roles must stay then
        if service_exists(target):
            outcome = _Moved.TAKEN
            return outcome
        if not service_exists(source):
            return outcome
        # As when a service is registered (cf. `_register_service()`); the
        # new identifier is locked, and isn't registered
        remove_permissions(target)
        result = get_collection("services").update_one(
            {"id": source}, {"$set": {"id": target, UPDATED: format_time(now())}}
        )
        if result.matched_count:
            outcome = _Moved.MOVED
    except DuplicateKeyError:
        outcome = _Moved.TAKEN
    finally:
        if outcome is not _Moved.MOVED:
            _unlock(source, target, tokens)
    return outcome


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


def resume_renames() -> None:
    """Resume renames whose locks are stale, and remove other stale locks.

    Renames are finished or rolled back (cf. `_finish()`), each once, also
    by concurrent calls (cf. `_claim()`); failures are logged, so that one
    rename doesn't block others or the app (it is tried again by the next
    call). Stale locks of changes, and locks of new identifiers without a
    lock of their rename, are removed.

    Called when the app starts and before requests (cf.
    `resume_renames_periodically()`); must be called in an app context.
    """
    collection = get_collection(LOCKS)
    stale = {"started_at": {"$lte": stale_before()}}
    for lock in collection.find({"to": {"$exists": True}, **stale}):
        token = _claim(lock)
        if token is None:
            continue
        try:
            _finish(lock["path"], lock["_id"], lock["to"], token)
        except Exception:
            logger.exception(
                "Interrupted rename of service '%s' to '%s' not resumed; "
                "tried again later.",
                escape_log_value(lock["_id"]),
                escape_log_value(lock["to"]),
            )
    for lock in collection.find({"from": {"$exists": True}, **stale}):
        if not collection.count_documents(
            {"_id": lock["from"], "to": lock["_id"]}, limit=1
        ):
            unlock(lock["_id"], lock["token"])
    remove_stale_changes()


def resume_renames_periodically() -> None:
    """Resume interrupted renames, at most every `RESUME_INTERVAL` seconds.

    Called before each request (cf. `cloud_registry.app.create_app()`), so
    that interrupted renames are resumed soon after their locks are stale,
    not only when the app starts.
    """
    global _last_resume  # noqa: PLW0603 (per worker)
    current = time.monotonic()
    if _last_resume is not None and current - _last_resume < RESUME_INTERVAL:
        return
    _last_resume = current
    resume_renames()


def _claim(lock: dict[str, Any]) -> str | None:
    """Claim an interrupted rename, so that only one call resumes it.

    The token of the lock is the compare-and-swap value: a new one is set,
    with the current time as start, unless the token changed since the lock
    was read (i.e., another call claimed it).

    Args:
        lock: Lock of the old identifier, as read.

    Returns:
        New token of the lock, or `None` if another call claimed it.
    """
    token = uuid4().hex
    claimed = get_collection(LOCKS).find_one_and_update(
        {"_id": lock["_id"], "token": lock["token"]},
        {"$set": {"token": token, "started_at": now()}},
    )
    return None if claimed is None else token


def _finish(path: str, source: str, target: str, token: str) -> None:
    """Finish or roll back an interrupted rename.

    - Only the new identifier is registered: the roles are moved.
    - The old one is still registered: rolled back, i.e., the locks released
      (nothing else changed before the service moved).
    - Both are registered (e.g., the new one by `POST /services`): rolled
      back, with a warning, as roles might otherwise move to another service.
    - Neither is (the service was deleted after it moved): the roles left on
      the old identifier are removed.

    Args:
        path: Path of the services, under which roles are granted.
        source: Old identifier.
        target: New identifier.
        token: Token of the claimed lock of the old identifier.
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
    get_collection(LOCKS).delete_one({"_id": target, "from": source})
    unlock(source, token)
