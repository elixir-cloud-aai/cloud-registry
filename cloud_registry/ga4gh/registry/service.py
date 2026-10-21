"""Controller for registering services."""

from copy import deepcopy
import ipaddress
import json
import logging
import re
import string
import unicodedata
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit

from foca.utils.misc import generate_id
import idna
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import (
    BadRequest,
    Conflict,
    InternalServerError,
    NotFound,
)
from cloud_registry.utils import escape_log_value, get_collection, get_custom_config

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
DEFAULT_PORTS = {"http": 80, "https": 443}
# Schemes of service URLs and links
HTTP_SCHEMES = frozenset(DEFAULT_PORTS)
# Categories of characters rejected in URLs (cf. `_check_characters()`):
# control and format characters, surrogates (unpaired; they can't be
# encoded) and separators
REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Zs"})
# Format characters allowed in international host names, where IDNA 2008
# allows them in some contexts (CONTEXTJ): zero width non-joiner and joiner
JOINERS = frozenset("\u200c\u200d")
# Fields with URLs of links, as paths of keys, with the schemes they allow
# besides `http` and `https`: contact URLs can be email addresses (RFC 2368),
# as in the GA4GH service info specification
LINK_FIELDS: dict[tuple[str, ...], frozenset[str]] = {
    ("contactUrl",): frozenset({"mailto"}),
    ("documentationUrl",): frozenset(),
    ("organization", "url"): frozenset(),
}
PERCENT_ENCODED = re.compile(r"%([0-9A-Fa-f]{2})")
# Percent signs not starting a percent-encoding
MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# IPv6 zone identifiers (RFC 6874), after `%25`
ZONE_ID = re.compile(r"(?:[A-Za-z0-9\-._~]|%[0-9A-Fa-f]{2})+")
# Labels of domain names (letters, digits, hyphens; RFC 1123, section 2.1)
LDH_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
# Registered names (RFC 3986, section 3.2.2)
REG_NAME = re.compile(r"(?:[A-Za-z0-9\-._~!$&'()*+,;=]|%[0-9A-Fa-f]{2})*")
# RFC 3986, section 2.3
UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
# Printable ASCII characters, kept when percent-encoding non-ASCII ones
ASCII_PRINTABLE = "".join(chr(code) for code in range(33, 127))


class DuplicateService(Conflict):
    """A service with the same URL and API type is registered.

    Attributes:
        existing_id: Identifier of the registered service.
    """

    def __init__(self, existing_id: str) -> None:
        super().__init__(
            f"A service with the same URL and API type is registered: '{existing_id}'."
        )
        self.existing_id = existing_id


class InvalidService(BadRequest):
    """The URL or API type of a service, or a link, is missing or invalid."""


def normalize_url(url: str) -> str:
    """Normalize a service URL for comparisons.

    Normalizes common spellings: lowercases the scheme and host, encodes
    international host names (IDNA), writes IPv6 addresses in canonical form,
    percent-encodes non-ASCII characters of paths, and removes trailing dots
    of hosts, default ports, dot segments, repeated and trailing slashes,
    needless percent-encoding of paths, queries and fragments. `http` and
    `https` URLs are different services.

    Args:
        url: Service URL; leading and trailing spaces are ignored.

    Returns:
        Normalized URL.

    Raises:
        InvalidService: The URL is invalid (cf. `_split_http_url()`) or has
            malformed percent-encodings in its path.
    """
    # surrounding spaces are tolerated for backward compatibility; links are
    # stored as written and must not have any (cf. `check_link()`)
    parts, host = _split_http_url(url.strip(" "), subject="Service URLs")
    scheme = parts.scheme.lower()
    if parts.port is not None and parts.port != DEFAULT_PORTS[scheme]:
        host = f"{host}:{parts.port}"
    return urlunsplit((scheme, host, _normalize_path(parts.path), "", ""))


def check_strings(value: object, field: str = "") -> None:
    """Check that the strings of a request body can be stored.

    Unpaired surrogates (e.g., from JSON escapes like `\\ud800`) can't be
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


def check_link(url: object, field: str, schemes: frozenset[str] = frozenset()) -> None:
    """Check that the URL of a link is valid.

    Links are not compared, so not normalized, but checked like service URLs
    (cf. `_split_http_url()`).

    Args:
        url: URL of the link.
        field: Name of the field with the URL, for error messages.
        schemes: Schemes allowed besides `http` and `https`, for URLs with a
            path (e.g., an email address) but no authority (`//host`).

    Raises:
        InvalidService: The URL is not a string, or invalid: neither an
            HTTP(S) URL (cf. `_split_http_url()`) nor a URL with one of
            `schemes`, email addresses (cf. `_check_addresses()`) and no
            authority.
    """
    subject = f"URLs in '{field}'"
    if not isinstance(url, str):
        raise InvalidService(f"{subject} must be strings.")
    scheme, _, rest = url.partition(":")
    # without an authority, `urlsplit()` doesn't raise
    if scheme.lower() in schemes and not rest.startswith("//") and urlsplit(url).path:
        _check_characters(url, subject=subject)
        _check_percent_encodings(url, subject=subject)
        _check_addresses(urlsplit(url).path, subject=subject)
        return
    _split_http_url(url, subject=subject)


def check_links(data: dict) -> None:
    """Check the URLs of the links of a service (cf. `LINK_FIELDS`).

    Args:
        data: Service metadata.

    Raises:
        InvalidService: A URL is invalid (cf. `check_link()`).
    """
    for path, schemes in LINK_FIELDS.items():
        value: object = data
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            check_link(value, field=".".join(path), schemes=schemes)


def _check_characters(
    url: str, subject: str, allowed: frozenset[str] = frozenset()
) -> None:
    """Check that a URL has no whitespace, control or format characters.

    URLs are stored as written, so such characters are rejected rather than
    normalized: `urlsplit()` would strip some of them or drop tabs and
    newlines, so that the stored URL would differ from the one checked.
    Format characters (e.g., bidirectional controls, soft hyphens) are
    invisible or reorder text, so they are rejected, too, except `allowed`
    ones (e.g., `JOINERS` in hosts, which the IDNA check validates).

    Args:
        url: URL, or part of it.
        subject: Subject of error messages (e.g., `Service URLs`).
        allowed: Characters allowed although in `REJECTED_CATEGORIES`.

    Raises:
        InvalidService: The URL has whitespace, control or format
            characters, other than `allowed` ones.
    """
    if any(
        char not in allowed
        and (char.isspace() or unicodedata.category(char) in REJECTED_CATEGORIES)
        for char in url
    ):
        raise InvalidService(
            f"{subject} must not contain whitespace, control or format characters."
        )


def _check_percent_encodings(text: str, subject: str) -> None:
    """Check that the percent signs in a URL, or part of it, start
    percent-encodings.

    Args:
        text: URL, or part of it.
        subject: Subject of error messages (e.g., `Service URLs`).

    Raises:
        InvalidService: The text has malformed percent-encodings.
    """
    if MALFORMED_PERCENT.search(text):
        raise InvalidService(f"{subject} must not contain malformed percent-encodings.")


def _check_addresses(path: str, subject: str) -> None:
    """Check the email addresses of a `mailto:` URL (minimally, not as of
    RFC 5322).

    Args:
        path: Path of the URL, with comma-separated addresses.
        subject: Subject of error messages.

    Raises:
        InvalidService: An address has no local part, one with `@` (unless
            percent-encoded), or no domain name (cf. `_check_domain()`).
    """
    for address in path.split(","):
        local, _, domain = address.rpartition("@")
        if not local or "@" in local or not domain:
            raise InvalidService(f"{subject} need valid email addresses.")
        _check_domain(domain, subject=subject)


def _check_domain(domain: str, subject: str) -> None:
    """Check the domain of an email address.

    Args:
        domain: Domain.
        subject: Subject of error messages.

    Raises:
        InvalidService: The domain is not a domain name (e.g., an IP address,
            or not valid per IDNA 2008): after IDNA encoding, it needs at
            least two labels of letters, digits and hyphens (1-63 characters,
            no leading or trailing hyphen), and at most 253 characters.
    """
    message = f"{subject} need email addresses with a domain name."
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise InvalidService(message)
    if not domain.isascii():
        try:
            domain = idna.encode(domain, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidService(message) from exc
    labels = domain.split(".")
    if (
        len(domain) > 253
        or len(labels) < 2
        or not all(LDH_LABEL.fullmatch(label) for label in labels)
    ):
        raise InvalidService(message)


def _split_http_url(url: str, subject: str) -> tuple[SplitResult, str]:
    """Split an HTTP(S) URL, checking it.

    Args:
        url: URL.
        subject: Subject of error messages (e.g., `Service URLs`).

    Returns:
        Parts of the URL, and its normalized host (cf. `_normalize_host()`).

    Raises:
        InvalidService: The URL is invalid, has characters rejected by
            `_check_characters()` (joiners only in the host), malformed
            percent-encodings, no scheme or an invalid or no host, a scheme
            other than `http` and `https`, user info, or an invalid port (or
            `0`).
    """
    _check_characters(url, subject=subject, allowed=JOINERS)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise InvalidService(f"{subject} must be valid URLs: {exc}.") from exc
    for part in (parts.path, parts.query, parts.fragment):
        _check_characters(part, subject=subject)
        _check_percent_encodings(part, subject=subject)
    if parts.scheme and parts.scheme.lower() not in HTTP_SCHEMES:
        raise InvalidService(f"{subject} must be HTTP(S) URLs.")
    if not parts.scheme or not parts.hostname:
        raise InvalidService(f"{subject} need a scheme and a host.")
    if parts.username is not None or parts.password is not None:
        raise InvalidService(f"{subject} must not contain user info.")
    if port == 0:
        raise InvalidService(f"{subject} need a valid port.")
    return parts, _normalize_host(parts.hostname, subject=subject)


def _normalize_host(host: str, subject: str) -> str:
    """Normalize the host of a URL.

    Args:
        host: Lowercase host, without brackets for IPv6 addresses.
        subject: Subject of error messages.

    Returns:
        Host without trailing dots; IPv6 addresses in canonical form, in
        brackets; international host names encoded (IDNA 2008, UTS 46).

    Raises:
        InvalidService: The host is invalid or empty, e.g., an IPv6 address
            with a `%` not followed by `25` and a zone identifier (`ZONE_ID`,
            non-empty).
    """
    if ":" in host:
        address, percent, zone = host.partition("%")
        try:
            address = ipaddress.IPv6Address(address).compressed
        except ValueError as exc:
            raise InvalidService(f"{subject} need a valid host: {exc}.") from exc
        if not percent:
            return f"[{address}]"
        if not zone.startswith("25") or not ZONE_ID.fullmatch(zone[2:]):
            raise InvalidService(f"{subject} need a valid host: '{host}'.")
        return f"[{address}%{zone}]"
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidService(f"{subject} need a valid host: {exc}.") from exc
    if not REG_NAME.fullmatch(host):
        raise InvalidService(f"{subject} need a valid host: '{host}'.")
    host = host.rstrip(".")
    if not host:
        raise InvalidService(f"{subject} need a scheme and a host.")
    return host


def _normalize_path(path: str) -> str:
    """Normalize the path of a URL (cf. RFC 3986, section 6.2.2).

    Args:
        path: URL path.

    Returns:
        Path with non-ASCII characters percent-encoded (UTF-8), unreserved
        characters decoded, other percent-encodings in uppercase, dot
        segments resolved, and without repeated and trailing
        slashes. Malformed percent-encodings are rejected beforehand (cf.
        `_split_http_url()`).
    """
    path = PERCENT_ENCODED.sub(_decode_unreserved, quote(path, safe=ASCII_PRINTABLE))
    segments: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if segments:
                segments.pop()
        elif segment not in ("", "."):
            segments.append(segment)
    return "".join(f"/{segment}" for segment in segments)


def _decode_unreserved(match: re.Match) -> str:
    """Decode a percent-encoded character if it is unreserved.

    Args:
        match: Match of a percent-encoded character (`PERCENT_ENCODED`).

    Returns:
        The character if it is unreserved, otherwise its percent-encoding in
        uppercase.
    """
    char = chr(int(match.group(1), 16))
    return char if char in UNRESERVED else match.group(0).upper()


def service_key(data: dict) -> str:
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
        data: dict,
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
                link, or a string (cf. `check_strings()`) is invalid.
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
        check_links(self.data)
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
                # dot segments, which clients resolve in URLs
                if self.data["id"] in (".", ".."):
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
            query: dict = {"id": self.data["id"]}
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
        query: dict = {KEY: key}
        if self.data["id"] is not None:
            query["id"] = {"$ne": self.data["id"]}
        existing = self.db_coll.find_one(query, {"_id": False, "id": True})
        if existing is not None:
            raise DuplicateService(existing_id=existing["id"])


def add_service_keys() -> None:
    """Add keys to services registered without one, e.g., before keys were
    introduced.

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
            "Fix or delete these services (cf. 'Duplicate services' in the "
            f"README): {'; '.join(problems)}"
        )
