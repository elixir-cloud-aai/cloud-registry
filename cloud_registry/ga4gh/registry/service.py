"""Controller for registering services."""

from copy import deepcopy
import logging
from urllib.parse import urlsplit, urlunsplit

from foca.utils.misc import generate_id
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


def normalize_url(url: str) -> str:
    """Normalize a service URL for comparisons.

    Lowercases the scheme and host, and removes default ports, trailing
    slashes, queries and fragments.

    Args:
        url: Service URL.

    Returns:
        Normalized URL.

    Raises:
        cloud_registry.exceptions.BadRequest: The URL has no scheme or host,
            an invalid port, or user info.
    """
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError as exc:
        raise BadRequest(f"Invalid port in service URL '{url}'.") from exc
    if not parts.scheme or not parts.hostname:
        raise BadRequest(f"Service URL '{url}' has no scheme or host.")
    if parts.username is not None or parts.password is not None:
        raise BadRequest("Service URLs must not contain user info.")
    scheme = parts.scheme.lower()
    host = parts.hostname  # lowercased, without brackets for IPv6
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parts.path.rstrip("/"), "", ""))


def service_key(data: dict) -> str:
    """Get the key identifying a service by its URL and API type.

    Args:
        data: Service metadata, with `url` and `type`.

    Returns:
        Key from the normalized URL and the case-insensitive API type
        (`type.group`, `type.artifact`), but not its version, so that
        upgrading a service to a new API version updates its entry.

    Raises:
        cloud_registry.exceptions.BadRequest: The URL or type is missing or
            invalid.
    """
    try:
        url, group, artifact = (
            data["url"],
            data["type"]["group"],
            data["type"]["artifact"],
        )
    except (KeyError, TypeError) as exc:
        raise BadRequest("Services need a URL and a type.") from exc
    return " ".join((group.lower(), artifact.lower(), normalize_url(url)))


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
        document = {**self.data, KEY: key}

        # replace or insert service with provided ID (PUT)
        if self.replace:
            try:
                result = self.db_coll.replace_one(
                    filter={"id": self.data["id"]},
                    replacement=document,
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
                try:
                    # insert a copy, as `insert_one()` adds an `_id` field
                    self.db_coll.insert_one(
                        document={**document, "id": self.data["id"]}
                    )
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
            and API type; the message names them.
    """
    collection = get_collection("services")
    for service in collection.find({KEY: {"$exists": False}}, {"_id": False}):
        try:
            key = service_key(service)
        except BadRequest as exc:
            raise RuntimeError(
                f"Service '{service['id']}' has an invalid URL or type "
                f"({exc.description}); fix or delete it."
            ) from exc
        try:
            collection.update_one({"id": service["id"]}, {"$set": {KEY: key}})
        except DuplicateKeyError as exc:
            existing = collection.find_one({KEY: key}, {"_id": False, "id": True})
            raise RuntimeError(
                f"Services '{service['id']}' and "
                f"'{(existing or {}).get('id')}' have the same URL and API "
                "type; delete one of them."
            ) from exc
        logger.info("Added key to service '%s'.", service["id"])
