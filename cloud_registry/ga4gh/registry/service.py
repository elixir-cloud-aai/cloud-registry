"""Controller for registering services."""

from collections.abc import Collection
from copy import deepcopy
import json
import logging
from typing import Any

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
    Conflict,
    InternalServerError,
    NotFound,
)
from cloud_registry.utils import (
    get_collection,
    get_custom_config,
    is_date_time,
)

logger = logging.getLogger(__name__)
# Internal field identifying a service by its normalized URL and API type
# (not version), with a unique index (cf. `config.yaml`); not served
KEY = "_serviceKey"
# Internal field with the time an admin verified a service (cf.
# `verification.py`); removed when its URL (as written) or key changes; not
# served
VERIFIED = "_verifiedAt"
# Projection excluding internal fields
PROJECTION = {"_id": False, KEY: False, VERIFIED: False}
# Fields with URLs of links, as paths of keys, with the schemes they allow
# (cf. `foca.utils.urls.check_url_fields()`): contact URLs can be email
# addresses (RFC 6068), as in the GA4GH service info specification
LINK_FIELDS: dict[str, frozenset[str]] = {
    "contactUrl": HTTP_SCHEMES | {"mailto"},
    "documentationUrl": HTTP_SCHEMES,
    "organization.url": HTTP_SCHEMES,
}
# Fields with timestamps (`format: date-time`), which Connexion's request
# validation doesn't check without an optional package (`rfc3339-validator`)
DATE_TIME_FIELDS = ("createdAt", "updatedAt")
# Identifiers that can't be used for services: dot segments, which clients
# resolve in URLs, and `types`, as `/services/types` lists the service types
# (cf. the pattern of `serviceId` of `PUT` in `additions.openapi.yaml`)
RESERVED_IDS = frozenset({".", "..", "types"})


class DuplicateService(Conflict):
    """A service with the same URL and API type is registered.

    Attributes:
        existing_id: Identifier of the registered service.
    """

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
    ) -> None:
        """Initialize service data.

        Args:
            data: Service metadata consistent with the
                `ExternalServiceRegister` schema.
            id: Service identifier. Auto-generated if not provided.
            allow_create: With an identifier, whether the service may be
                registered if it doesn't exist (e.g., for admins only, cf.
                `_replace()`); otherwise, only an existing service is
                replaced.

        Attributes:
            data: Service metadata; a copy of `data`, with the identifier.
            replace: Whether an existing service with the provided identifier
                should be replaced. Set to `True` if an `id` is provided,
                otherwise set to `False`.
            was_created: Whether a new service was registered.
            verification_removed: Whether the verification of a replaced
                service was removed (cf. `_replace()`).
            allow_create: Whether a service with the identifier may be
                registered.
            id_charset: A string of allowed characters for generating service
                identifiers.
            id_length: Length of generated service identifiers.
            url_schemes: Schemes allowed in service URLs.
            db_coll: Database collection for storing service objects.
        """
        endpoint_conf = get_custom_config().endpoints
        self.data = deepcopy(data)
        self.data["id"] = id
        self.replace = id is not None
        self.allow_create = allow_create
        self.was_created = False
        self.verification_removed = False
        self.id_charset: str = endpoint_conf.services.id.charset
        self.id_length = int(endpoint_conf.services.id.length)
        self.url_schemes = frozenset(endpoint_conf.services.url_schemes)
        self.db_coll = get_collection("services")

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
                URL is not allowed (cf. `check_url_scheme()`).
            DuplicateService: Another service with the same URL and API type
                is registered.
            InternalServerError: The service could not be registered within
                the retries.
            cloud_registry.exceptions.NotFound: With an identifier, the
                service doesn't exist (also if it was deleted concurrently),
                and `allow_create` is false.
        """
        check_strings(self.data)
        key = service_key(self.data)
        check_url_scheme(self.data["url"], schemes=self.url_schemes)
        check_links(self.data)
        check_date_times(self.data)
        self._check_duplicate(key=key)

        # replace or insert service with provided ID (PUT)
        if self.replace:
            self._replace(key=key, retries=retries)

        # insert service with random ID (POST); new ID if key exists
        else:
            for _ in range(retries + 1):
                self.data["id"] = generate_id(
                    charset=self.id_charset, length=self.id_length
                )
                if self.data["id"] in RESERVED_IDS:
                    continue
                try:
                    # insert a new dict, as `insert_one()` adds an `_id` field
                    self.db_coll.insert_one(document={**self.data, KEY: key})
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

    def _replace(self, key: str, retries: int) -> None:
        """Replace the service with the provided identifier, or insert it.

        The verification of the service (cf. `VERIFIED`) is kept if its URL
        is unchanged, exactly as written, and so is its key (API type);
        otherwise, it is removed in the same write. Other spellings of the
        URL (e.g., with a query, which normalized URLs, and so keys, don't
        have) remove it, too: the admin checked the URL as written. To this
        end, the stored service is read first, and only replaced if neither
        its URL, key nor verification changed meanwhile (compare-and-swap);
        otherwise, it is read again. So a verification is never kept for
        another URL or API type, and verifications concurrent with a
        replacement keeping both are not lost. Only if the service is
        inserted concurrently (after it was read as missing) is a
        verification of the inserted service removed.

        The service is only inserted if `allow_create` is set, also when it
        was deleted after it was read (e.g., by a concurrent `DELETE`), so
        that users who may only update a service can't register it again
        (and become its owner).

        Args:
            key: Key of the service (cf. `service_key()`).
            retries: How many times the service is read again if it changed
                concurrently.

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
                {"_id": False, "url": True, KEY: True, VERIFIED: True},
            )
            if stored is None and not self.allow_create:
                raise NotFound
            replacement = {**self.data, KEY: key}
            query: dict[str, Any] = {"id": self.data["id"]}
            if stored is not None:
                # the fields as read; missing if they were (e.g., services
                # without key, cf. `add_service_keys()`)
                for field in ("url", KEY, VERIFIED):
                    query[field] = stored.get(field, {"$exists": False})
                unchanged = (
                    stored.get("url") == self.data["url"] and stored.get(KEY) == key
                )
                if unchanged and VERIFIED in stored:
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
        url = service.get("url")
        if isinstance(url, str):
            try:
                normalize_url(url)
                continue
            except InvalidService:
                pass
        ids.append(f"'{service['id']}'")
    if ids:
        logger.warning(
            "Services with invalid URLs are still served, but can only be "
            "updated with a valid URL, and duplicates of them may not be "
            "detected; update or delete them: %s",
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
    schemes = frozenset(get_custom_config().endpoints.services.url_schemes)
    ids = []
    for service in get_collection("services").find(
        {}, {"_id": False, "id": True, "url": True}
    ):
        url = service.get("url")
        if not isinstance(url, str):
            continue
        try:
            normalize_url(url)
        except InvalidService:
            # invalid URLs stored before URLs were checked can't be updated
            # unchanged anyway (cf. "Stored services" in `docs/upgrading.md`)
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
