"""Controller for registering services."""

from collections.abc import Collection, Iterable
from copy import deepcopy
import json
import logging
from typing import Any

from foca import get_custom_config
from foca.errors import FieldError, json_pointer
from foca.utils.logging import escape_log_value
from foca.utils.misc import generate_id
from foca.utils.service_info import is_uri
from foca.utils.urls import HTTP_SCHEMES, InvalidURL, check_url
from foca.utils.urls import normalize_url as normalize_http_url
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import (
    INVALID_FORMAT,
    INVALID_URL,
    INVALID_VALUE,
    SCHEME_NOT_ALLOWED,
    CodedConflict,
    InternalServerError,
    InvalidFields,
    NotFound,
)
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import (
    format_time,
    get_collection,
    is_date_time,
    now,
)

logger = logging.getLogger(__name__)
# Internal field identifying a service by its normalized URL and API type
# (not version), with a unique index (cf. `config.yaml`); not served
KEY = "_serviceKey"
# Internal field with the time an admin verified a service (cf.
# `verification.py`); removed when its URL (as written) or key changes; not
# served
VERIFIED = "_verifiedAt"
# Internal field of pending services: `True` while the type of the service
# is pending (cf. `cloud_registry.ga4gh.registry.service_types`), with an
# index, so that reads filter by it; derived from the type, not served
PENDING_SERVICE = "_pending"
# Internal field with the user who registered the service, for the limit of
# services per user (cf. `cloud_registry.ga4gh.registry.service_limits`);
# `None` for services that count for nobody (e.g., registered by admins);
# kept on updates and renames; not served
CREATED_BY = "_createdBy"
# Internal field of the entry of the registry among its own services (cf.
# `cloud_registry.ga4gh.registry.self_listing`): `True`; kept on updates and
# renames; not served
SELF_ENTRY = "_selfEntry"
# Internal field with the state of the sync of a service with its service info
# (cf. `cloud_registry.ga4gh.registry.sync`), while it is turned on; kept on
# updates and renames; not served
SYNC = "_sync"
# Projection excluding internal fields
PROJECTION = {
    "_id": False,
    KEY: False,
    VERIFIED: False,
    PENDING_SERVICE: False,
    CREATED_BY: False,
    SELF_ENTRY: False,
    SYNC: False,
}
# Fields with URLs of links, as paths of keys, with the schemes they allow
# (cf. `link_errors()`): contact URLs can be email
# addresses (RFC 6068), as in the GA4GH service info specification
LINK_FIELDS: dict[str, frozenset[str]] = {
    "contactUrl": HTTP_SCHEMES | {"mailto"},
    "documentationUrl": HTTP_SCHEMES,
    "organization.url": HTTP_SCHEMES,
}
# Timestamps of services, which the registry sets (cf. `RegisterService`):
# when the service was registered, and when it was last changed
CREATED = "createdAt"
UPDATED = "updatedAt"
# Fields with timestamps (`format: date-time`); FOCA checks those of requests,
# the app those it doesn't get through requests
DATE_TIME_FIELDS = (CREATED, UPDATED)
# Identifiers that can't be used for services: dot segments, which clients
# resolve in URLs, and `types`, as `/services/types` lists the service types
# (cf. the pattern of `serviceId` of `PUT`, and of `ServiceRename`, in
# `additions.openapi.yaml`)
RESERVED_IDS = frozenset({".", "..", "types"})
# Detail of invalid fields with identifiers that can't be stored in the
# policy rules of the roles on services (code `unusable-id`; cf.
# `cloud_registry.ga4gh.registry.rename.check_id()`)
UNUSABLE_ID_DETAIL = (
    "Can't be used for services: must not contain commas or line breaks, and "
    "must not be too long."
)


class HiddenDuplicateService(CodedConflict):
    """A service with the same URL and API type is registered (not named)."""

    problem_code = "duplicate-service"


class DuplicateService(CodedConflict):
    """A service with the same URL and API type is registered.

    The description names the registered service; for users who may not
    read it, responses use `hidden()` instead.

    Attributes:
        HIDDEN_DESCRIPTION: Description without the registered service.
        existing_id: Identifier of the registered service.
    """

    problem_code = HiddenDuplicateService.problem_code
    HIDDEN_DESCRIPTION = "A service with the same URL and API type is registered."

    def hidden(self) -> HiddenDuplicateService:
        """Get the conflict without the registered service.

        Returns:
            Conflict whose description doesn't name the registered service,
            with the same code.
        """
        return HiddenDuplicateService(self.HIDDEN_DESCRIPTION)

    def __init__(self, existing_id: str) -> None:
        """Create the exception for a service with the same URL and API type.

        Args:
            existing_id: Identifier of the registered service.
        """
        super().__init__(
            f"A service with the same URL and API type is registered: '{existing_id}'."
        )
        self.existing_id = existing_id


class SyncUrlLocked(CodedConflict):
    """The URL of a service with sync turned on must stay."""

    problem_code = "sync-url-locked"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(
            "Service info is synced from the URL of this service, so it can't "
            "change. To move the service, turn sync off first, or import it anew."
        )


class InvalidService(InvalidFields):
    """The URL or API type of a service, or another field, is invalid.

    Has the invalid fields (cf. `cloud_registry.exceptions.InvalidFields`),
    with pointers into the service, unless it is missing its URL or type,
    or has strings that can't be stored (cf. `check_strings()`), which the
    schema of requests rejects before.
    """


# Name of URLs in the messages of FOCA's URL checks, which start with it (e.g.,
# `URLs must not contain user info.`) and quote values, if at all, after a
# colon (e.g., `URLs need a valid host: '...'.`)
_URL_NAME = "URLs"
# Reasons of FOCA's URL checks whose details are worded for a single URL
_URL_REASONS = {
    "must be strings": "Must be a string.",
    "must be valid URLs": "Must be a valid URL.",
}
# Start of the reason of FOCA's URL checks for schemes that aren't allowed
_SCHEME_REASON = "must use one of these schemes"


def _url_error(exc: InvalidURL, schemes: Collection[str], pointer: str) -> FieldError:
    """Convert an error of FOCA's URL checks to an invalid field.

    Args:
        exc: Error of a check with the name `_URL_NAME`.
        schemes: Allowed schemes.
        pointer: Pointer to the field of the URL.

    Returns:
        The invalid field: `scheme-not-allowed`, naming the allowed schemes,
        or `invalid-url`, with the reason of the check up to its first colon,
        so without the values it may quote (e.g., `Needs a valid host.`).
    """
    reason = str(exc).removeprefix(f"{_URL_NAME} ").split(":", 1)[0].rstrip(".")
    if reason.startswith(_SCHEME_REASON):
        allowed = ", ".join(f"'{scheme}'" for scheme in sorted(schemes))
        return FieldError(
            detail=f"Must use one of these schemes: {allowed}.",
            code=SCHEME_NOT_ALLOWED,
            pointer=pointer,
        )
    detail = _URL_REASONS.get(reason)
    if detail is None:
        if reason.startswith("need "):
            reason = f"needs {reason.removeprefix('need ')}"
        detail = f"{reason[:1].upper()}{reason[1:]}."
    return FieldError(detail=detail, code=INVALID_URL, pointer=pointer)


def url_errors(url: object, schemes: Collection[str], *path: str) -> list[FieldError]:
    """Check a URL as written (cf. `foca.utils.urls.check_url()`).

    Args:
        url: URL.
        schemes: Allowed schemes, lowercase.
        *path: Keys of the path to the URL in the checked object, for the
            pointer of the error.

    Returns:
        Nothing for a valid URL with an allowed scheme; otherwise, its error:
        `scheme-not-allowed` for a URL with a scheme that isn't allowed
        (case-insensitive), `invalid-url` for other invalid URLs, saying
        why. The details don't quote the URL.
    """
    try:
        check_url(url, schemes=schemes, name=_URL_NAME)
    except InvalidURL as exc:
        return [_url_error(exc, schemes, json_pointer(*path))]
    return []


def normalize_url(url: str, field: str = "url") -> str:
    """Normalize a service URL for comparisons.

    See `foca.utils.urls.normalize_url()`: common spellings are normalized,
    and queries and fragments removed; `http` and `https` URLs are different
    services.

    Args:
        url: Service URL.
        field: Field of the URL in the checked object, for the error.

    Returns:
        Normalized URL.

    Raises:
        InvalidService: The URL is invalid, or not an HTTP(S) URL (cf.
            `url_errors()`).
    """
    try:
        return normalize_http_url(url, name=_URL_NAME)
    except InvalidURL as exc:
        raise InvalidService(
            field_errors=[_url_error(exc, HTTP_SCHEMES, json_pointer(field))]
        ) from exc


def check_strings(value: object, field: str = "") -> None:
    r"""Check that the strings of a request body can be stored.

    Unpaired surrogates (e.g., from JSON escapes like `\ud800`) can't be
    encoded (UTF-8), so they fail when stored or logged. FOCA rejects request
    bodies with them; other values (e.g., fetched service info) are checked
    here.

    Args:
        value: Request body, or part of it.
        field: Path of `value` in the body, as dotted keys (e.g.,
            `type.group`), for error messages.

    Raises:
        InvalidService: A string, or a key, has unpaired surrogates (with
            the invalid field, code `invalid-value`, unless it's `value`
            itself without a `field`).
    """
    _check_strings(value, tuple(field.split(".")) if field else ())


def _check_strings(value: object, path: tuple[str | int, ...]) -> None:
    """Check that the strings of a value can be encoded (cf. `check_strings()`).

    Args:
        value: Request body, or part of it.
        path: Keys and indices of `value` in the body.

    Raises:
        InvalidService: A string, or a key, has unpaired surrogates.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            # keys are checked before they're used in paths
            _check_strings(key, path)
            _check_strings(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_strings(item, (*path, index))
    elif isinstance(value, str):
        try:
            value.encode()
        except UnicodeEncodeError as exc:
            if not path:
                raise InvalidService(
                    "The request body must not contain unpaired surrogates."
                ) from exc
            field = "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in path
            ).removeprefix(".")
            raise InvalidService(
                f"'{field}' must not contain unpaired surrogates.",
                field_errors=[
                    FieldError(
                        detail="Must not contain unpaired surrogates.",
                        code=INVALID_VALUE,
                        pointer=json_pointer(*path),
                    )
                ],
            ) from exc


def date_time_errors(data: dict[str, Any]) -> list[FieldError]:
    """Check that the timestamps of a service are RFC 3339 date-times.

    FOCA's schema validation (`format: date-time`) rejects most invalid
    timestamps of requests first; this check also covers those it lets
    through, and values that aren't requests (e.g., fetched service info).

    Args:
        data: Service metadata.

    Returns:
        An error (`invalid-format`) per timestamp (cf. `DATE_TIME_FIELDS`)
        that is not an RFC 3339 date-time.
    """
    return [
        FieldError(
            detail="Must be an RFC 3339 date-time (e.g., '2019-06-04T12:58:19Z').",
            code=INVALID_FORMAT,
            pointer=json_pointer(field),
        )
        for field in DATE_TIME_FIELDS
        if field in data
        and not (isinstance(data[field], str) and is_date_time(data[field]))
    ]


def environment_errors(
    data: dict[str, Any], environments: list[str]
) -> list[FieldError]:
    """Check that the environment of a service is allowed.

    Args:
        data: Service metadata.
        environments: Allowed values (cf.
            `custom.endpoints.services.environments`); any if empty.

    Returns:
        An error (`invalid-value`) if the service has an environment
        (`environment`) that is not one of `environments` (case-sensitive).
    """
    if not environments or "environment" not in data:
        return []
    if data["environment"] in environments:
        return []
    allowed = ", ".join(f"'{value}'" for value in environments)
    return [
        FieldError(
            detail=f"Must be one of: {allowed}.",
            code=INVALID_VALUE,
            pointer=json_pointer("environment"),
        )
    ]


def link_errors(data: dict[str, Any]) -> list[FieldError]:
    """Check the URLs of the links of a service (cf. `LINK_FIELDS`).

    Links are not compared, so not normalized, but checked as written (cf.
    `url_errors()`). Missing links (also if a parent is missing or not an
    object) are not checked; links set to `None` are rejected.

    Args:
        data: Service metadata.

    Returns:
        An error per link that is invalid or uses a scheme its field doesn't
        allow.
    """
    errors = []
    for field, schemes in LINK_FIELDS.items():
        path = field.split(".")
        value: object = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            errors.extend(url_errors(value, schemes, *path))
    return errors


def service_key(data: dict[str, Any]) -> str:
    """Get the key identifying a service by its URL and API type.

    Args:
        data: Service metadata, with `url` and `type`.

    Returns:
        Key from the API type (`type.group`, `type.artifact`,
        case-insensitive and without surrounding whitespace) and the
        normalized URL, as a JSON array, but not the version, so that
        upgrading a service to a new API version updates its entry.

    Raises:
        InvalidService: The URL or type is missing or invalid.
    """
    url, group, artifact = _key_values(data)
    return json.dumps(
        [group.strip().casefold(), artifact.strip().casefold(), normalize_url(url)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _key_values(data: dict[str, Any]) -> tuple[str, str, str]:
    """Get the values that identify a service.

    Args:
        data: Service metadata.

    Returns:
        The URL, `type.group` and `type.artifact`.

    Raises:
        InvalidService: The URL or type is missing, or not strings.
    """
    try:
        url, group, artifact = (
            data["url"],
            data["type"]["group"],
            data["type"]["artifact"],
        )
    except (KeyError, TypeError) as exc:
        raise InvalidService("Services need a URL and a type.") from exc
    if not all(isinstance(value, str) for value in (url, group, artifact)):
        raise InvalidService("Service URLs and types must be strings.")
    return url, group, artifact


def service_exists(id: str) -> bool:
    """Check whether a service is registered.

    Args:
        id: Service identifier.

    Returns:
        Whether a service with identifier `id` is registered.
    """
    return get_collection("services").count_documents({"id": id}, limit=1) > 0


class RegisterService:
    """Class for registering services with the registry."""

    def __init__(
        self,
        data: dict[str, Any],
        id: str | None = None,
        allow_create: bool = True,
        keep_timestamps: bool = False,
    ) -> None:
        """Initialize service data.

        The registry sets the timestamps of services (cf. `DATE_TIME_FIELDS`):
        `createdAt` when a service is registered, kept on updates, and
        `updatedAt` when it is registered or updated. Timestamps in `data`
        are ignored, unless `keep_timestamps` is set.

        Args:
            data: Service metadata consistent with the
                `ExternalServiceRegister` schema.
            id: Service identifier. Auto-generated if not provided.
            allow_create: With an identifier, whether the service may be
                registered if it doesn't exist (e.g., for admins only, cf.
                `_replace()`); otherwise, only an existing service is
                replaced.
            keep_timestamps: Whether timestamps in `data` are stored as sent
                (e.g., for admins importing services), instead of those set
                by the registry; timestamps not in `data` are set as usual.

        Attributes:
            data: Service metadata; a copy of `data`, with the identifier, and
                without timestamps unless `keep_timestamps` is set; the
                timestamps set by the registry are added on registration.
            replace: Whether an existing service with the provided identifier
                should be replaced. Set to `True` if an `id` is provided,
                otherwise set to `False`.
            was_created: Whether a new service was registered.
            verification_removed: Whether the verification of a replaced
                service was removed (cf. `_replace()`).
            pending: Whether the service is stored as pending (cf.
                `PENDING_SERVICE`); set before `register_metadata()`.
            created_by: User the service counts for if it is registered
                (cf. `CREATED_BY`), `None` for nobody; set before
                `register_metadata()`. Replaced services keep theirs.
            allow_create: Whether a service with the identifier may be
                registered.
            id_charset: A string of allowed characters for generating service
                identifiers.
            id_length: Length of generated service identifiers.
            url_schemes: Schemes allowed in service URLs.
            environments: Values allowed for `environment`; any if empty.
            sync: Whether the deployment syncs services with their service info.
            db_coll: Database collection for storing service objects.
        """
        endpoint_conf = get_custom_config(CustomConfig).endpoints
        self.data = deepcopy(data)
        if not keep_timestamps:
            for field in DATE_TIME_FIELDS:
                self.data.pop(field, None)
        self.data["id"] = id
        self.replace = id is not None
        self.allow_create = allow_create
        self.was_created = False
        self.verification_removed = False
        self.pending = False
        self.created_by: str | None = None
        self.id_charset: str = endpoint_conf.services.id.charset
        self.id_length = int(endpoint_conf.services.id.length)
        self.url_schemes = frozenset(endpoint_conf.services.url_schemes)
        self.environments: list[str] = list(endpoint_conf.services.environments)
        self.sync = endpoint_conf.services.service_info.sync
        self.db_coll = get_collection("services")

    def check(self, errors: Iterable[FieldError] = ()) -> str:
        """Check the values of the service, whatever else is registered.

        All invalid fields are reported together: the URL (cf.
        `url_errors()`), the links (cf. `link_errors()`), the timestamps (cf.
        `date_time_errors()`) and the environment (cf.
        `environment_errors()`).

        Args:
            errors: Invalid fields that other checks found (e.g., of the
                service type), reported together with those of the service.

        Returns:
            Key of the service (cf. `service_key()`).

        Raises:
            InvalidService: Cf. `register_metadata()`.
        """
        check_strings(self.data)
        # raises for a missing or invalid key (`url`, `type`), which the
        # other checks need
        _key_values(self.data)
        found = [
            *url_errors(self.data["url"], self.url_schemes, "url"),
            *link_errors(self.data),
            *date_time_errors(self.data),
            *environment_errors(self.data, environments=self.environments),
            *errors,
        ]
        if found:
            raise InvalidService(field_errors=found)
        return service_key(self.data)

    def validate(self) -> str:
        """Check the service, before it is registered.

        Also called by `register_metadata()`; callers may call it first, so
        that they only act (e.g., store its type) for valid services.

        Returns:
            Key of the service (cf. `service_key()`).

        Raises:
            InvalidService: Cf. `register_metadata()`.
            DuplicateService: Another service with the same URL and API type
                is registered.
            SyncUrlLocked: The update changes the URL of a service with sync.
        """
        key = self.check()
        self._check_duplicate(key=key)
        if self.replace:
            stored = self.db_coll.find_one(
                {"id": self.data["id"]}, {"_id": False, "url": True, SYNC: True}
            )
            if stored is not None:
                self._check_url_lock(stored)
        return key

    def _check_url_lock(self, stored: dict[str, Any]) -> None:
        """Check that an update keeps the URL of a service with sync.

        Args:
            stored: Stored service, with `url` and `SYNC`.

        Raises:
            SyncUrlLocked: The service has sync, and another URL, as written.
        """
        if self.sync and SYNC in stored and stored.get("url") != self.data["url"]:
            raise SyncUrlLocked

    def _internal_fields(self, key: str) -> dict[str, Any]:
        """Get the internal fields to store with the service.

        Args:
            key: Key of the service (cf. `service_key()`).

        Returns:
            The key, the user the service counts for (cf. `CREATED_BY`), and
            the pending flag if the service is pending (cf.
            `PENDING_SERVICE`).
        """
        fields = {KEY: key, CREATED_BY: self.created_by}
        return fields | ({PENDING_SERVICE: True} if self.pending else {})

    def register_metadata(self, retries: int = 9) -> None:
        """Register service.

        Args:
            retries: Without a service identifier (POST), how many times
                the generation of a random identifier and insertion into the
                database are retried on `DuplicateKeyError`s. With an
                identifier (PUT), how many times the stored service is read
                again if it changed concurrently (cf. `_replace()`).

        Raises:
            InvalidService: The URL or API type of the service, the URL of a
                link, a timestamp or a string (cf. `check_strings()`) is
                invalid, or the scheme of the service URL or the environment
                is not allowed (cf. `check()`).
            DuplicateService: Another service with the same URL and API type
                is registered.
            InternalServerError: The service could not be registered within
                the retries.
            cloud_registry.exceptions.NotFound: With an identifier, the
                service doesn't exist (also if it was deleted concurrently),
                and `allow_create` is false.
        """
        key = self.validate()
        timestamp = format_time(now())

        # replace or insert service with provided ID (PUT)
        if self.replace:
            self._replace(key=key, retries=retries, timestamp=timestamp)

        # insert service with random ID (POST); new ID if key exists
        else:
            self.data.setdefault(CREATED, timestamp)
            self.data.setdefault(UPDATED, timestamp)
            for _ in range(retries + 1):
                self.data["id"] = generate_id(
                    charset=self.id_charset, length=self.id_length
                )
                if self.data["id"] in RESERVED_IDS:
                    continue
                try:
                    # insert a new dict, as `insert_one()` adds an `_id` field
                    self.db_coll.insert_one(
                        document={**self.data, **self._internal_fields(key)}
                    )
                except DuplicateKeyError:
                    # the key may have been registered concurrently
                    self._check_duplicate(key=key)
                    continue
                self.was_created = True
                logger.info(
                    "Added service with id '%s'.", escape_log_value(self.data["id"])
                )
                break
            else:
                raise InternalServerError
        logger.debug(
            "Entry in 'services' collection: %s",
            escape_log_value(
                self.db_coll.find_one({"id": self.data["id"]}, PROJECTION)
            ),
        )

    def _replace(self, key: str, retries: int, timestamp: str) -> None:
        """Replace the service with the provided identifier, or insert it.

        The verification of the service (cf. `VERIFIED`) is kept if its URL
        is unchanged, exactly as written, and so is its key (API type), and
        the service isn't stored as pending (`pending`, e.g., changed to a
        new version of its type); otherwise, it is removed in the same
        write. Other spellings of the URL (e.g., with a query, which
        normalized URLs, and so keys, don't have) remove it, too: the admin
        checked the URL as written. To this end, the stored service is read
        first, and only replaced if neither its URL, key nor verification
        changed meanwhile (compare-and-swap); otherwise, it is read again.
        So a verification is never kept for another URL or API type, and
        verifications concurrent with a replacement keeping both are not
        lost. Only if the service is inserted concurrently (after it was
        read as missing) is a verification of the inserted service removed.

        The service is only inserted if `allow_create` is set, also when it
        was deleted after it was read (e.g., by a concurrent `DELETE`), so
        that users who may only update a service can't register it again
        (and become its owner).

        `createdAt` is kept, unless it is in `data` (cf. `keep_timestamps`
        of `__init__()`), and set to `timestamp` if the service is inserted;
        it is compared like the URL, so that it isn't reverted if it is
        changed concurrently. Stored services without a valid one (an RFC
        3339 date-time; e.g., registered before the registry set it) are kept
        without it. `updatedAt` is set
        to `timestamp`, unless the service is inserted with one in `data`
        (e.g., by an admin importing it): replaced services always get a new
        one, also if sent back as read (e.g., by admins editing them).

        Args:
            key: Key of the service (cf. `service_key()`).
            retries: How many times the service is read again if it changed
                concurrently.
            timestamp: Time of the request (RFC 3339), for `updatedAt`, and
                `createdAt` of inserted services.

        Raises:
            DuplicateService: Another service with the same URL and API type
                was registered concurrently.
            cloud_registry.exceptions.InternalServerError: The service
                changed or was added concurrently on each try.
            cloud_registry.exceptions.NotFound: The service doesn't exist
                (anymore), and `allow_create` is false.
            SyncUrlLocked: Sync was turned on after `validate()`, and the update
                changes the URL.
        """
        for _ in range(retries + 1):
            stored = self.db_coll.find_one(
                {"id": self.data["id"]},
                {
                    "_id": False,
                    "url": True,
                    KEY: True,
                    VERIFIED: True,
                    CREATED: True,
                    CREATED_BY: True,
                    SELF_ENTRY: True,
                    SYNC: True,
                },
            )
            if stored is None and not self.allow_create:
                raise NotFound
            replacement = {**self.data, **self._internal_fields(key)}
            if stored is not None or UPDATED not in replacement:
                replacement[UPDATED] = timestamp
            query: dict[str, Any] = {"id": self.data["id"]}
            if stored is None:
                replacement.setdefault(CREATED, timestamp)
            else:
                # the fields as read; missing if they were
                for field in (
                    "url",
                    KEY,
                    VERIFIED,
                    CREATED,
                    CREATED_BY,
                    SELF_ENTRY,
                    SYNC,
                ):
                    query[field] = stored.get(field, {"$exists": False})
                # sync may have been turned on since `validate()`
                self._check_url_lock(stored)
                # the user the service counts for never changes
                replacement[CREATED_BY] = stored[CREATED_BY]
                # the entry of the registry stays marked as such
                if SELF_ENTRY in stored:
                    replacement[SELF_ENTRY] = stored[SELF_ENTRY]
                # sync stays on, and overrides the values at its next attempt;
                # a new URL (with the deployment's sync off, cf. `_check_url_lock()`)
                # would never have been fetched by the user who turned it on
                if SYNC in stored and stored["url"] == self.data["url"]:
                    replacement[SYNC] = stored[SYNC]
                # invalid ones (e.g., set by owners before timestamps were
                # checked) are removed
                created = stored.get(CREATED)
                if isinstance(created, str) and is_date_time(created):
                    replacement.setdefault(CREATED, created)
                unchanged = (
                    stored.get("url") == self.data["url"] and stored.get(KEY) == key
                )
                # pending services aren't verified (cf. `PENDING_SERVICE`)
                if unchanged and VERIFIED in stored and not self.pending:
                    replacement[VERIFIED] = stored[VERIFIED]
            try:
                result = self.db_coll.replace_one(
                    filter=query,
                    replacement=replacement,
                    upsert=stored is None,
                )
            except DuplicateKeyError:
                # another service with the key was registered concurrently;
                # otherwise, the service itself was (e.g., by a concurrent
                # upsert with the identifier), so it is read again
                self._check_duplicate(key=key)
                logger.debug(
                    "Service '%s' added concurrently.",
                    escape_log_value(self.data["id"]),
                )
                continue
            if result.matched_count or result.upserted_id is not None:
                self.was_created = result.upserted_id is not None
                self.verification_removed = (
                    stored is not None
                    and VERIFIED in stored
                    and VERIFIED not in replacement
                )
                return
            logger.debug(
                "Service '%s' changed concurrently.", escape_log_value(self.data["id"])
            )
        raise InternalServerError

    def _check_duplicate(self, key: str) -> None:
        """Check that no other service has the same key.

        Args:
            key: Key of the service (cf. `service_key()`).

        Raises:
            DuplicateService: Another service has the key.
        """
        query: dict[str, Any] = {KEY: key}
        if self.data["id"] is not None:
            query["id"] = {"$ne": self.data["id"]}
        existing = self.db_coll.find_one(query, {"_id": False, "id": True})
        if existing is not None:
            raise DuplicateService(existing_id=existing["id"])


# Fields of the organization of a service (cf. `ExternalServiceRegister` in
# `api/additions.openapi.yaml`, which rejects others)
ORGANIZATION_FIELDS = frozenset({"name", "url"})


def warn_environments() -> None:
    """Warn of stored services whose environments are not allowed.

    For example, after the environments allowed changed
    (`custom.endpoints.services.environments`). Such services are still
    served, but can only be updated with an allowed environment (or none).
    """
    environments = get_custom_config(CustomConfig).endpoints.services.environments
    if not environments:
        return
    ids = []
    for service in get_collection("services").find(
        {"environment": {"$exists": True}},
        {"_id": False, "id": True, "environment": True},
    ):
        if environment_errors(service, environments=environments):
            ids.append(f"'{service['id']}'")
    if ids:
        logger.warning(
            "Services with environments not allowed "
            "(`custom.endpoints.services.environments`) are still served, but "
            "can only be updated with an allowed environment (or none); update "
            "or delete them: %s",
            escape_log_value(", ".join(ids)),
        )


def warn_url_schemes() -> None:
    """Warn of stored services whose URLs use schemes that are not allowed.

    For example, `http` URLs registered while `http` was allowed
    (`custom.endpoints.services.url_schemes`).
    Such services are still served, but can only be updated with an allowed
    scheme.
    """
    schemes = frozenset(get_custom_config(CustomConfig).endpoints.services.url_schemes)
    ids = []
    for service in get_collection("services").find(
        {}, {"_id": False, "id": True, "url": True}
    ):
        if url_errors(service["url"], schemes, "url"):
            ids.append(f"'{service['id']}'")
    if ids:
        logger.warning(
            "Services with URL schemes not allowed "
            "(`custom.endpoints.services.url_schemes`) are still served, but "
            "can only be updated with an allowed scheme; update or delete them: %s",
            escape_log_value(", ".join(ids)),
        )


# Fields of services with URLs (`format: uri`)
URI_FIELDS = ("url", "contactUrl", "documentationUrl")


def warn_invalid_formats() -> None:
    """Warn of stored services that FOCA's response checks would reject.

    FOCA checks the `date-time` and `uri` formats of responses, too, so a
    single stored service with an invalid value makes its reads and
    `GET /services` fail with `500`. Requests can't store such values, but data
    written around the API can.
    """
    ids = []
    for service in get_collection("services").find(
        {},
        {
            "_id": False,
            "id": True,
            "url": True,
            "contactUrl": True,
            "documentationUrl": True,
            "organization": True,
            CREATED: True,
            UPDATED: True,
        },
    ):
        organization = service.get("organization", {})
        if not isinstance(organization, dict):
            ids.append(f"'{service.get('id')}'")
            continue
        uris = [service[field] for field in URI_FIELDS if field in service]
        if "url" in organization:
            uris.append(organization["url"])
        dates = [service[field] for field in DATE_TIME_FIELDS if field in service]
        if not all(isinstance(value, str) and is_uri(value) for value in uris) or (
            not all(isinstance(value, str) and is_date_time(value) for value in dates)
        ):
            ids.append(f"'{service.get('id')}'")
    if ids:
        logger.error(
            "Services with invalid URLs or timestamps make reads of them and "
            "`GET /services` return 500; correct or delete them in the database: %s",
            escape_log_value(", ".join(ids)),
        )
