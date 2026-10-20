"""Controller for registering services."""

from copy import deepcopy
import ipaddress
import json
import logging
import re
import string
from urllib.parse import quote, urlsplit, urlunsplit

from foca.utils.misc import generate_id
import idna
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import BadRequest, Conflict, InternalServerError
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)
# Internal field identifying a service by its normalized URL and API type
# (not version), with a unique index (cf. `config.yaml`); not served
KEY = "_serviceKey"
# Projection excluding internal fields
PROJECTION = {"_id": False, KEY: False}
DEFAULT_PORTS = {"http": 80, "https": 443}
PERCENT_ENCODED = re.compile(r"%([0-9A-Fa-f]{2})")
# Percent signs not starting a percent-encoding
MALFORMED_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
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
    """The URL or API type of a service is missing or invalid."""


def normalize_url(url: str) -> str:
    """Normalize a service URL for comparisons.

    Normalizes common spellings: lowercases the scheme and host, encodes
    international host names (IDNA), writes IPv6 addresses in canonical form,
    percent-encodes non-ASCII characters of paths, and removes trailing dots
    of hosts, default ports, dot segments, repeated and trailing slashes,
    needless percent-encoding of paths, queries and fragments. Other schemes
    (e.g., `http` and `https`) give other URLs.

    Args:
        url: Service URL.

    Returns:
        Normalized URL.

    Raises:
        InvalidService: The URL is invalid, has no scheme or an invalid or no
            host, has user info, or malformed percent-encodings in its path.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise InvalidService(f"Invalid service URL: {exc}.") from exc
    if not parts.scheme or not parts.hostname:
        raise InvalidService("Service URLs need a scheme and a host.")
    if parts.username is not None or parts.password is not None:
        raise InvalidService("Service URLs must not contain user info.")
    scheme = parts.scheme.lower()
    host = _normalize_host(parts.hostname)
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, _normalize_path(parts.path), "", ""))


def _normalize_host(host: str) -> str:
    """Normalize the host of a URL.

    Args:
        host: Lowercase host, without brackets for IPv6 addresses.

    Returns:
        Host without trailing dots; IPv6 addresses in canonical form, in
        brackets; international host names encoded (IDNA 2008, UTS 46).

    Raises:
        InvalidService: The host is invalid or empty.
    """
    if ":" in host:
        address, _, zone = host.partition("%")
        try:
            address = ipaddress.IPv6Address(address).compressed
        except ValueError as exc:
            raise InvalidService(f"Invalid host in service URL: {exc}.") from exc
        return f"[{address}%{zone}]" if zone else f"[{address}]"
    if not host.isascii():
        try:
            host = idna.encode(host, uts46=True, transitional=False).decode()
        except idna.IDNAError as exc:
            raise InvalidService(f"Invalid host in service URL: {exc}.") from exc
    if not REG_NAME.fullmatch(host):
        raise InvalidService(f"Invalid host in service URL: '{host}'.")
    host = host.rstrip(".")
    if not host:
        raise InvalidService("Service URLs need a scheme and a host.")
    return host


def _normalize_path(path: str) -> str:
    """Normalize the path of a URL (cf. RFC 3986, section 6.2.2).

    Args:
        path: URL path.

    Returns:
        Path with non-ASCII characters percent-encoded (UTF-8), unreserved
        characters decoded, other percent-encodings in uppercase, dot
        segments resolved, and without repeated and trailing
        slashes.

    Raises:
        InvalidService: The path has malformed percent-encodings.
    """
    if MALFORMED_PERCENT.search(path):
        raise InvalidService("Invalid percent-encoding in service URL path.")
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
    ) -> None:
        """Initialize service data.

        Args:
            data: Service metadata consistent with the
            `ExternalServiceRegister` schema.
            id: Service identifier. Auto-generated if not provided.

        Attributes:
            data: Service metadata; a copy of `data`, with the identifier.
            replace: Whether an existing service with the provided identifier
                should be replaced. Set to `True` if an `id` is provided,
                otherwise set to `False`.
            was_created: Whether a new service was registered.
            id_charset: A string of allowed characters for generating service
                identifiers.
            id_length: Length of generated service identifiers.
            db_coll: Database collection for storing service objects.
        """
        endpoint_conf = get_custom_config().endpoints
        self.data = deepcopy(data)
        self.data["id"] = id
        self.replace = id is not None
        self.was_created = False
        self.id_charset: str = endpoint_conf.services.id.charset
        self.id_length = int(endpoint_conf.services.id.length)
        self.db_coll = get_collection("services")

    def register_metadata(self, retries: int = 9) -> None:
        """Register service.

        Args:
            retries: How many times should the generation of a random
                identifier and insertion into the database be retried when
                encountering `DuplicateKeyError`s if a service identifier was
                not provided.

        Raises:
            DuplicateService: Another service with the same URL and API type
                is registered.
        """
        key = service_key(self.data)
        self._check_duplicate(key=key)

        # replace or insert service with provided ID (PUT)
        if self.replace:
            try:
                result = self.db_coll.replace_one(
                    filter={"id": self.data["id"]},
                    replacement={**self.data, KEY: key},
                    upsert=True,
                )
            except DuplicateKeyError:
                # registered concurrently
                self._check_duplicate(key=key)
                raise
            self.was_created = result.upserted_id is not None

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
                logger.info("Added service with id '%s'.", self.data["id"])
                break
            else:
                raise InternalServerError
        logger.debug(
            "Entry in 'services' collection: %s",
            self.db_coll.find_one({"id": self.data["id"]}, PROJECTION),
        )

    def _check_duplicate(self, key: str) -> None:
        """Check that no other service has the same key.

        Args:
            key: Key of the service (cf. `service_key()`).

        Raises:
            DuplicateService: Another service has the key.
        """
        filter: dict = {KEY: key}
        if self.data["id"] is not None:
            filter["id"] = {"$ne": self.data["id"]}
        existing = self.db_coll.find_one(filter, {"_id": False, "id": True})
        if existing is not None:
            raise DuplicateService(existing_id=existing["id"])


def add_service_keys() -> None:
    """Add keys to services registered without one, e.g., before keys were
    introduced.

    Raises:
        RuntimeError: Services have invalid URLs or types, or the same URL
            and API type; the message names all of them. Assumes that no
            services without keys are registered concurrently (e.g., by an
            app of an older version).
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
            logger.info("Added key to service '%s'.", service["id"])
    if problems:
        raise RuntimeError(
            "Fix or delete these services (cf. 'Duplicate services' in the "
            f"README): {'; '.join(problems)}"
        )
