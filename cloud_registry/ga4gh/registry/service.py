"""Controller for registering services."""

from collections.abc import Collection
from copy import deepcopy
import json
import logging
from typing import Any

from foca import get_custom_config
from foca.utils.logging import escape_log_value
from foca.utils.misc import generate_id
from foca.utils.urls import (
    HTTP_SCHEMES,
    InvalidURL,
    check_url,
    check_url_fields,
)
from foca.utils.urls import normalize_url as normalize_http_url
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import (
    BadRequest,
    CodedConflict,
    InternalServerError,
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
# Projection excluding internal fields
PROJECTION = {
    "_id": False,
    KEY: False,
    VERIFIED: False,
    PENDING_SERVICE: False,
    CREATED_BY: False,
}
# Fields with URLs of links, as paths of keys, with the schemes they allow
# (cf. `foca.utils.urls.check_url_fields()`): contact URLs can be email
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
# Fields with timestamps (`format: date-time`), which Connexion's request
# validation doesn't check without an optional package (`rfc3339-validator`)
DATE_TIME_FIELDS = (CREATED, UPDATED)
# Identifiers that can't be used for services: dot segments, which clients
# resolve in URLs, and `types`, as `/services/types` lists the service types
# (cf. the pattern of `serviceId` of `PUT`, and of `ServiceRename`, in
# `additions.openapi.yaml`)
RESERVED_IDS = frozenset({".", "..", "types"})


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


class InvalidService(BadRequest):
    """The URL or API type of a service, or a link, is missing or invalid."""


def normalize_url(url: str) -> str:
    """Normalize a service URL for comparisons.

    See `foca.utils.urls.normalize_url()`: common spellings are normalized,
    and queries and fragments removed; `http` and `https` URLs are different
    services.

    Args:
        url: Service URL; leading and trailing spaces are ignored.

    Returns:
        Normalized URL.

    Raises:
        InvalidService: The URL is invalid, or not an HTTP(S) URL.
    """
    # surrounding spaces are tolerated for backward compatibility; links are
    # stored as written and must not have any (cf. `check_links()`)
    try:
        return normalize_http_url(url.strip(" "), name="Service URLs")
    except InvalidURL as exc:
        raise InvalidService(str(exc)) from exc


def check_url_scheme(url: str, schemes: Collection[str]) -> None:
    """Check that a service URL is valid and uses one of the allowed schemes.

    See `foca.utils.urls.check_url()`, which checks the whole URL, like
    `normalize_url()`; for URLs that `normalize_url()` accepts, only the
    scheme can fail the check.

    Args:
        url: Service URL; leading and trailing spaces are ignored.
        schemes: Allowed schemes, lowercase (cf.
            `custom.endpoints.services.url_schemes`).

    Raises:
        InvalidService: The URL is invalid, or its scheme is not one of
            `schemes` (case-insensitive).
    """
    try:
        check_url(url.strip(" "), schemes=schemes, name="Service URLs")
    except InvalidURL as exc:
        raise InvalidService(str(exc)) from exc


def check_strings(value: object, field: str = "") -> None:
    r"""Check that the strings of a request body can be stored.

    Unpaired surrogates (e.g., from JSON escapes like `\ud800`) can't be
    encoded (UTF-8), so they fail when stored or logged.

    Args:
        value: Request body, or part of it.
        field: Path of `value` in the body (e.g., `type.group`), for error
            messages.

    Raises:
        InvalidService: A string, or a key, has unpaired surrogates.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            check_strings(key, field=field)
            check_strings(item, field=f"{field}.{key}" if field else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_strings(item, field=f"{field}[{index}]")
    elif isinstance(value, str):
        try:
            value.encode()
        except UnicodeEncodeError as exc:
            where = f"'{field}'" if field else "The request body"
            raise InvalidService(
                f"{where} must not contain unpaired surrogates."
            ) from exc


def check_date_times(data: dict[str, Any]) -> None:
    """Check that the timestamps of a service are RFC 3339 date-times.

    Args:
        data: Service metadata.

    Raises:
        InvalidService: A timestamp (cf. `DATE_TIME_FIELDS`) is not an RFC
            3339 date-time.
    """
    for field in DATE_TIME_FIELDS:
        if field in data and not (
            isinstance(data[field], str) and is_date_time(data[field])
        ):
            raise InvalidService(
                f"'{field}' must be an RFC 3339 date-time (e.g., "
                "'2019-06-04T12:58:19Z')."
            )


def check_environment(data: dict[str, Any], environments: list[str]) -> None:
    """Check that the environment of a service is allowed.

    Args:
        data: Service metadata.
        environments: Allowed values (cf.
            `custom.endpoints.services.environments`); any if empty.

    Raises:
        InvalidService: The service has an environment (`environment`) that
            is not one of `environments` (case-sensitive).
    """
    if not environments or "environment" not in data:
        return
    if data["environment"] not in environments:
        allowed = ", ".join(f"'{value}'" for value in environments)
        raise InvalidService(f"'environment' must be one of: {allowed}.")


def check_links(data: dict[str, Any]) -> None:
    """Check the URLs of the links of a service (cf. `LINK_FIELDS`).

    Links are not compared, so not normalized, but checked as written (cf.
    `foca.utils.urls.check_url()`).

    Args:
        data: Service metadata.

    Raises:
        InvalidService: A URL is invalid, or uses a scheme the field doesn't
            allow; the message names the field.
    """
    try:
        check_url_fields(data, LINK_FIELDS)
    except InvalidURL as exc:
        raise InvalidService(str(exc)) from exc


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
    return json.dumps(
        [group.strip().casefold(), artifact.strip().casefold(), normalize_url(url)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


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
        self.db_coll = get_collection("services")

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
        """
        check_strings(self.data)
        key = service_key(self.data)
        check_url_scheme(self.data["url"], schemes=self.url_schemes)
        check_links(self.data)
        check_date_times(self.data)
        check_environment(self.data, environments=self.environments)
        self._check_duplicate(key=key)
        return key

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
                link, a timestamp (cf. `check_date_times()`) or a string (cf.
                `check_strings()`) is invalid, or the scheme of the service
                URL (cf. `check_url_scheme()`) or the environment (cf.
                `check_environment()`) is not allowed.
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
                # the fields as read; missing if they were (e.g., services
                # without key, cf. `add_service_keys()`, or without the user
                # they count for, cf. `add_service_creators()`)
                for field in ("url", KEY, VERIFIED, CREATED, CREATED_BY):
                    query[field] = stored.get(field, {"$exists": False})
                # the user the service counts for never changes (missing
                # until added)
                if CREATED_BY in stored:
                    replacement[CREATED_BY] = stored[CREATED_BY]
                else:
                    del replacement[CREATED_BY]
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


def add_service_keys() -> None:
    """Add keys to services registered without one.

    For example, to services registered before keys were introduced.

    Concurrent runs of this function are safe (e.g., in each worker of the
    app on startup): keys added by another run are kept. Services registered
    without keys while it runs (e.g., by an app of an older version) are not
    safe: they may be missed or reported as duplicates.

    Raises:
        RuntimeError: Services have invalid URLs or types, or the same URL
            and API type; the message names all of them.
    """
    collection = get_collection("services")
    problems = []
    for service in collection.find({KEY: {"$exists": False}}, {"_id": False}):
        try:
            key = service_key(service)
        except InvalidService as exc:
            problems.append(f"'{service['id']}': {exc.description}")
            continue
        existing = collection.find_one({KEY: key}, {"_id": False, "id": True})
        if existing is not None and existing["id"] == service["id"]:
            continue  # added concurrently, e.g., by another worker
        if existing is not None:
            problems.append(
                f"'{service['id']}': same URL and API type as '{existing['id']}'"
            )
            continue
        try:
            # unless the service was replaced concurrently, with a new key
            result = collection.update_one(
                {"id": service["id"], KEY: {"$exists": False}}, {"$set": {KEY: key}}
            )
        except DuplicateKeyError:
            # registered concurrently, e.g., by an app of an older version
            problems.append(f"'{service['id']}': same URL and API type as another")
            continue
        if result.modified_count:
            logger.info("Added key to service '%s'.", escape_log_value(service["id"]))
    if problems:
        raise RuntimeError(
            "Fix or delete these services (cf. https://cloud-registry.readthedocs.io"
            f"/en/latest/upgrading/#stored-services): {'; '.join(problems)}"
        )


def _is_valid_url(url: object) -> bool:
    """Check whether a stored service URL is valid.

    Args:
        url: Stored service URL.

    Returns:
        Whether the URL is a string that `normalize_url()` accepts.
    """
    if not isinstance(url, str):
        return False
    try:
        normalize_url(url)
    except InvalidService:
        return False
    return True


def warn_invalid_urls() -> None:
    """Warn of stored services with keys whose URLs are invalid.

    For example, URLs accepted before stricter checks were introduced (cf.
    "Stored services" in `docs/upgrading.md`). Such services are still
    served, but can only be updated with a valid URL, and their keys may
    differ from those of the same URL spelled validly, so registrations of
    the service with such a spelling are not detected as duplicates.
    Services without keys are checked when keys are added (cf.
    `add_service_keys()`).
    """
    ids = []
    for service in get_collection("services").find(
        {KEY: {"$exists": True}},
        {"_id": False, "id": True, "url": True},
        sort=[("id", 1)],
    ):
        if not _is_valid_url(service.get("url")):
            ids.append(f"'{service['id']}'")
    if ids:
        logger.warning(
            "Services with invalid URLs are still served, but can only be "
            "updated with a valid URL, and duplicates of them may not be "
            "detected; update or delete them: %s",
            escape_log_value(", ".join(ids)),
        )


def warn_environments() -> None:
    """Warn of stored services whose environments are not allowed.

    For example, of services registered before the environments were checked
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
        try:
            check_environment(service, environments=environments)
        except InvalidService:
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

    For example, `http` URLs registered before `https` became the only
    scheme allowed by default (`custom.endpoints.services.url_schemes`).
    Such services are still served, but can only be updated with an allowed
    scheme. Services with URLs that aren't strings or valid URLs (cf.
    `normalize_url()`) are skipped.
    """
    schemes = frozenset(get_custom_config(CustomConfig).endpoints.services.url_schemes)
    ids = []
    for service in get_collection("services").find(
        {}, {"_id": False, "id": True, "url": True}
    ):
        url = service.get("url")
        # invalid URLs can't be updated unchanged anyway (cf.
        # `warn_invalid_urls()`)
        if not isinstance(url, str) or not _is_valid_url(url):
            continue
        try:
            check_url_scheme(url, schemes=schemes)
        except InvalidService:
            ids.append(f"'{service['id']}'")
    if ids:
        logger.warning(
            "Services with URL schemes not allowed "
            "(`custom.endpoints.services.url_schemes`) are still served, but "
            "can only be updated with an allowed scheme; update or delete them: %s",
            escape_log_value(", ".join(ids)),
        )
