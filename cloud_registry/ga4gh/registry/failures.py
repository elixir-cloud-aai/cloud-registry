"""Failed operations: why imports, registrations, updates and syncs failed.

Admins and owners can't read the logs of the app, so failures of these
operations are recorded, for a limited time, and listed to those who may see
them (`GET /failures`): admins in admin mode see all, other users the failures
of what they did themselves and of the services they have a role on, also
failures that others caused (e.g., of syncs).

What is stored is fixed, so that failures hold no secrets: the time, the
operation, a reason from a fixed set (cf. `FailureReason`), the names of
invalid fields (never their values), the user who acted (none for the syncs of
the app), and the service, if it is known: its identifier, and its URL, only if
it is valid (URLs with user info, which could hold credentials, are invalid),
and without query and fragment, which could hold tokens.
Never response bodies, tokens or network errors.

Fetches that users start (imports, syncs on request) fail alike for them,
whatever the reason (cf. `cloud_registry.ga4gh.registry.service_import`), so
that they can't map networks; their failures say why to admins only, and
`fetch failed` to all others. The reasons of syncs of the app are served to
users with a role on the service already (`GET /services/{serviceId}/sync`), so
they see them here too.

MongoDB removes failures after
`custom.endpoints.services.failures.keep_days` (TTL index, cf.
`apply_keep_days()`), and each user's failures are only recorded up to
`custom.endpoints.services.failures.max_per_user_per_hour` (about: concurrent
requests can each record one more), so that nobody can fill the database by
failing on purpose. Recording never fails an operation:
errors are logged instead.
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bson import ObjectId
from foca import get_custom_config
from foca.errors import FieldError, InvalidFieldsProblem
from foca.security.access_control import get_current_subject, resource_filter

from cloud_registry.exceptions import field_name
from cloud_registry.ga4gh.registry.permissions import services_path
from cloud_registry.ga4gh.registry.service import InvalidService, url_errors
from cloud_registry.safe_fetch import AddressNotAllowed, FetchError
from cloud_registry.service_models.custom_config import CustomConfig, FailuresConfig
from cloud_registry.utils import (
    SECONDS_PER_DAY,
    format_time,
    get_collection,
    now,
    set_ttl_index,
)

logger = logging.getLogger(__name__)

# Collection of the failures, and the field of their time, with the TTL index
FAILURES = "failures"
OCCURRED_AT = "occurredAt"
# Window of the limit of failures recorded per user
WINDOW = timedelta(hours=1)
# Names of invalid fields stored per failure, at most; names are those of the
# API's fields and parameters (e.g., `organization.url`), longer ones aren't
FIELDS_PER_FAILURE = 10
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")
# Default and maximum size of a page of failures
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# Tokens of pages (cf. `_page_token()`), as the pattern of `pageToken` in the
# specification, which is checked with `re.search()`, where `$` also matches
# before a final line break, so tokens are checked again with this
PAGE_TOKEN = re.compile(r"[0-9]{1,15}\.[0-9a-f]{24}")


class Operation(StrEnum):
    """Operation that failed."""

    IMPORT = "import"
    REGISTER = "register"
    UPDATE = "update"
    SYNC = "sync"


class FailureReason(StrEnum):
    """Why an operation failed; served, so a fixed set."""

    # fetches (cf. `cloud_registry.safe_fetch`); served to admins only for
    # fetches that users start, which get `FETCH_FAILED` instead
    UNREACHABLE = "unreachable"
    ADDRESS_NOT_ALLOWED = "address not allowed"
    FETCH_FAILED = "fetch failed"
    # fetched service info
    NOT_JSON = "not JSON"
    INVALID_SERVICE_INFO = "invalid service info"
    IDENTITY_MISMATCH = "identity mismatch"
    # types of synced services (cf. `cloud_registry.ga4gh.registry.sync`)
    AWAITING_APPROVAL = "version awaiting approval"
    VERSION_NOT_ALLOWED = "version not allowed"
    TYPES_LIMIT = "unused types limit reached"
    # requests
    INVALID_FIELD = "invalid field"
    DUPLICATE_SERVICE = "duplicate service"
    SERVICES_LIMIT = "services limit reached"
    PENDING_SERVICE = "pending service"
    SYNC_URL_LOCKED = "url locked by sync"


def fetch_reason(exc: FetchError) -> FailureReason:
    """Get the reason of a failed fetch.

    Args:
        exc: Error of the fetch.

    Returns:
        `address not allowed`, or `unreachable` for all other errors.
    """
    if isinstance(exc, AddressNotAllowed):
        return FailureReason.ADDRESS_NOT_ALLOWED
    return FailureReason.UNREACHABLE


def invalid_fields(exc: InvalidService) -> list[str]:
    """Get the names of the invalid fields of an error, for a failure.

    Args:
        exc: Error with invalid fields.

    Returns:
        Their names (cf. `field_name()`), without duplicates, at most
        `FIELDS_PER_FAILURE`; names that don't look like names of the API's
        fields are left out, as they could come from the request.
    """
    names = [field_name(error) for error in exc.field_errors]
    field = getattr(exc, "field", None)
    if isinstance(field, str):
        names.append(field)
    valid = (name for name in names if _FIELD_NAME.fullmatch(name))
    return list(dict.fromkeys(valid))[:FIELDS_PER_FAILURE]


def _settings() -> FailuresConfig:
    """Get the settings of failures.

    Returns:
        `custom.endpoints.services.failures`.
    """
    return get_custom_config(CustomConfig).endpoints.services.failures


def apply_keep_days() -> None:
    """Apply the days for which failures are kept to the database.

    Sets the TTL index on `OCCURRED_AT` (cf. `set_ttl_index()`). Must be called
    in an app context, on startup.
    """
    seconds = _settings().keep_days * SECONDS_PER_DAY
    set_ttl_index(get_collection(FAILURES), OCCURRED_AT, seconds)


def _valid_url(url: object) -> str | None:
    """Get a URL of a service, if it is valid.

    Args:
        url: URL, as sent or stored.

    Returns:
        The URL without its query and fragment, which could hold tokens; `None`
        if it is invalid (e.g., with user info, which could hold credentials, or
        a scheme that isn't allowed).
    """
    services = get_custom_config(CustomConfig).endpoints.services
    if not isinstance(url, str) or url_errors(url, services.url_schemes):
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _over_limit(user: str) -> bool:
    """Check whether a user has as many recent failures as are recorded.

    Args:
        user: User identifier.

    Returns:
        Whether the user has `max_per_user_per_hour` failures in the last hour.
    """
    limit = _settings().max_per_user_per_hour
    query = {"userId": user, OCCURRED_AT: {"$gt": now() - WINDOW}}
    return get_collection(FAILURES).count_documents(query, limit=limit) >= limit


def record_failure(
    operation: Operation,
    reason: FailureReason,
    *,
    user: str | None = None,
    service: dict[str, Any] | None = None,
    url: object = None,
    fields: Iterable[str] = (),
    masked: bool = False,
) -> None:
    """Record a failed operation.

    Must be called in an app context. Errors are logged, not raised, so that
    they don't change the response of the operation.

    Args:
        operation: Operation.
        reason: Reason.
        user: User who acted; `None` for operations of the app (syncs).
        service: Stored service, with `_id`, `id` and `url`, if the operation
            was on one.
        url: URL of the service, if known (and not stored); only recorded if it
            is valid.
        fields: Names of invalid fields (cf. `invalid_fields()`).
        masked: Whether the reason is served to admins only, as a fetch that the
            user started failed (others get `fetch failed`).
    """
    try:
        if user is not None and _over_limit(user):
            logger.debug("Failure not recorded: limit of the user reached.")
            return
        failure: dict[str, Any] = {
            OCCURRED_AT: now(),
            "operation": str(operation),
            "reason": str(reason),
        }
        if masked:
            failure["masked"] = True
        if user is not None:
            failure["userId"] = user
        if service is not None:
            failure["serviceRef"] = service["_id"]
            failure["serviceId"] = service["id"]
            url = service.get("url")
        valid = _valid_url(url)
        if valid is not None:
            failure["url"] = valid
        names = list(fields)[:FIELDS_PER_FAILURE]
        if names:
            failure["fields"] = names
        get_collection(FAILURES).insert_one(failure)
    except Exception:
        logger.exception("Failure of operation '%s' not recorded.", operation)


def _visible() -> tuple[bool, dict[str, Any]]:
    """Get the filter for the failures that the current user may see.

    Must be called in a request context.

    Returns:
        Whether the user is an admin (in admin mode), and the filter: none for
        admins; for others, their own failures and those of the services they
        have a role on.
    """
    own = resource_filter(services_path())
    if not own:
        return True, {}
    refs = [item["_id"] for item in get_collection("services").find(own, {"_id": True})]
    return False, {
        "$or": [{"userId": get_current_subject()}, {"serviceRef": {"$in": refs}}]
    }


def _reason_filter(reason: str, admin: bool) -> dict[str, Any]:
    """Get the filter for failures with a reason, as the current user sees it.

    Args:
        reason: Reason.
        admin: Whether the user is an admin.

    Returns:
        Filter.
    """
    if reason == FailureReason.FETCH_FAILED:
        return {"masked": True}
    if admin:
        return {"reason": reason}
    return {"reason": reason, "masked": {"$exists": False}}


def _view(failure: dict[str, Any], admin: bool, user: str | None) -> dict[str, Any]:
    """Get a failure, as served to the current user.

    Args:
        failure: Stored failure.
        admin: Whether the user is an admin.
        user: Identifier of the user.

    Returns:
        The failure; the user who acted only for admins and the user
        themselves, and the reasons of failed fetches that users started only
        for admins (`fetch failed` otherwise).
    """
    reason = failure["reason"]
    if failure.get("masked") and not admin:
        reason = str(FailureReason.FETCH_FAILED)
    view: dict[str, Any] = {
        "id": str(failure["_id"]),
        "occurredAt": format_time(failure[OCCURRED_AT]),
        "operation": failure["operation"],
        "reason": reason,
    }
    for field in ("serviceId", "url", "fields"):
        if field in failure:
            view[field] = failure[field]
    if "userId" in failure and (admin or failure["userId"] == user):
        view["userId"] = failure["userId"]
    return view


def _page_token(failure: dict[str, Any]) -> str:
    """Get the token for the page after a failure.

    Args:
        failure: Stored failure, the last of a page.

    Returns:
        Its time, in milliseconds since the epoch, and its identifier, joined by
        a dot (cf. `_after()`).
    """
    occurred = failure[OCCURRED_AT]
    # naive, as read from the database
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    return f"{round(occurred.timestamp() * 1000)}.{failure['_id']}"


def _invalid_page_token() -> InvalidFieldsProblem:
    """Get the problem of a malformed page token.

    Tokens are opaque, and the registry issues none that fail the checks of
    `_after()`, so all get FOCA's code of pattern mismatches.

    Returns:
        `400 Bad Request` naming the parameter `pageToken`, without quoting
        its value.
    """
    return InvalidFieldsProblem(
        [
            FieldError(
                detail="Must be the 'nextPageToken' of a page.",
                code="pattern-mismatch",
                parameter="pageToken",
            )
        ]
    )


def _after(token: str) -> dict[str, Any]:
    """Get the filter for the failures after a page.

    Args:
        token: Token for the page (cf. `_page_token()`):
            `<milliseconds>.<identifier>`.

    Returns:
        Filter for older failures, and those of the same time with lower
        identifiers, in the order of the pages.

    Raises:
        foca.errors.InvalidFieldsProblem: The token is malformed (e.g., ends
            in a line break, which the schema's pattern lets through), or its
            time is out of range (after the year 9999); the problem details
            don't quote it.
    """
    if not PAGE_TOKEN.fullmatch(token):
        raise _invalid_page_token()
    milliseconds, identifier = token.split(".")
    try:
        occurred = datetime.fromtimestamp(int(milliseconds) / 1000, UTC)
    except (OverflowError, ValueError) as exc:
        raise _invalid_page_token() from exc
    return {
        "$or": [
            {OCCURRED_AT: {"$lt": occurred}},
            {OCCURRED_AT: occurred, "_id": {"$lt": ObjectId(identifier)}},
        ]
    }


# GET /failures
def getFailures(
    operation: str | None = None,
    reason: str | None = None,
    serviceId: str | None = None,
    pageSize: int = PAGE_SIZE,
    pageToken: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """List recent failed operations that the current user may see.

    Args:
        operation: Only list failures of this operation.
        reason: Only list failures with this reason, as served to the user.
        serviceId: Only list failures of the service with this identifier (at
            the time of the failure).
        pageSize: Maximum number of failures in the response.
        pageToken: `nextPageToken` of the previous page.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Failures, most recent first (cf. `_view()`; by identifier at the same
        time), and `nextPageToken` if there are more.
    """
    admin, query = _visible()
    conditions = [query] if query else []
    if operation is not None:
        conditions.append({"operation": operation})
    if reason is not None:
        conditions.append(_reason_filter(reason, admin))
    if serviceId is not None:
        conditions.append({"serviceId": serviceId})
    if pageToken is not None:
        conditions.append(_after(pageToken))
    found = list(
        get_collection(FAILURES)
        .find({"$and": conditions} if conditions else {})
        .sort([(OCCURRED_AT, -1), ("_id", -1)])
        .limit(pageSize + 1)
    )
    user = get_current_subject()
    page: dict[str, Any] = {
        "failures": [_view(item, admin, user) for item in found[:pageSize]]
    }
    if len(found) > pageSize:
        page["nextPageToken"] = _page_token(found[pageSize - 1])
    return page
