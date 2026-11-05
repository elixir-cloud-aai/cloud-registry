"""Importing service info: from the address of its endpoint to a registration.

Users enter the address of the service info of a service
(`POST /services/import`); the app fetches it, and returns the values for a
registration, which they can edit and register (`POST /services`). Nothing is
stored. The address must be the one of the service: `{url}/service-info`.

The app fetches addresses that users choose, so the fetch is limited (cf.
`cloud_registry.safe_fetch`): only public addresses, unless the deployment
permits networks (`custom.endpoints.services.service_info.private_networks`),
and never with a token. Requests need a login and are limited per user
(cf. `check_fetch_limit()`), and every failed fetch has the same response, so
that users can't map networks (cf. `FetchFailed`).

Only what a registration can have is taken from the service info, and it is
checked like a registration (cf. `service_values()`), except for what depends on
the user or on other services (e.g., duplicates), which `POST /services` checks.
"""

from datetime import timedelta
from functools import cache
import ipaddress
import json
import logging
from pathlib import Path
from typing import Any

from foca import get_custom_config
from foca.utils.logging import escape_log_value
from jsonschema import Draft4Validator
from pymongo.errors import DuplicateKeyError
import yaml

from cloud_registry.exceptions import BadGateway, TooManyRequests, problem
from cloud_registry.ga4gh.registry.permissions import current_user, require_subject
from cloud_registry.ga4gh.registry.service import (
    InvalidService,
    RegisterService,
    check_url_scheme,
    normalize_url,
)
from cloud_registry.safe_fetch import FetchError, IPNetwork, safe_get
from cloud_registry.service_models.custom_config import CustomConfig
from cloud_registry.utils import get_collection, now

logger = logging.getLogger(__name__)
# End of the addresses of service info, after the URL of the service (GA4GH)
SERVICE_INFO_PATH = "/service-info"
# Fields of service info that a registration takes as they are, and those of
# the objects it takes in part (cf. `ExternalServiceRegister` in
# `api/additions.openapi.yaml`, which rejects others)
SERVICE_FIELDS = (
    "name",
    "description",
    "contactUrl",
    "documentationUrl",
    "environment",
    "version",
)
OBJECT_FIELDS = {
    "type": ("group", "artifact", "version"),
    "organization": ("name", "url"),
}
# Maximum length of the identifier of service info, as of a name; the schema
# of registrations doesn't limit it, as they don't take it
MAX_ID_LENGTH = 1024
# Collection with the fetches of each user in the current hour (`count`, since
# `windowStart`), by user identifier (`_id`); MongoDB removes old ones (TTL
# index, cf. `config.yaml`)
FETCHES = "service_info_fetches"
FETCH_WINDOW = timedelta(hours=1)
# Request headers of fetches; `Accept` and others are set by `safe_get()`
FETCH_HEADERS = {"User-Agent": "cloud-registry"}
# Description of `FetchFailed`
FETCH_FAILED = (
    "The service info could not be fetched from this address. Only publicly "
    "available service info is supported: the registry sends no tokens, and "
    "doesn't follow redirects."
)
SPECIFICATION = Path(__file__).parents[2] / "api" / "additions.openapi.yaml"


class FetchFailed(BadGateway):
    """A fetch failed, whatever the reason, so that all look the same."""

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(FETCH_FAILED)


class InvalidServiceInfo(InvalidService):
    """The service info at an address doesn't make a valid registration."""


class FetchLimit(TooManyRequests):
    """A user has fetched as much service info as allowed for now."""

    def __init__(self, limit: int, retry_after: int) -> None:
        """Create the exception, with a description of what to do.

        Args:
            limit: How many fetches a user may start per hour.
            retry_after: Seconds until the user may fetch again.
        """
        super().__init__(
            f"You have fetched service info {limit} times in the last hour, as "
            "often as allowed; try again later.",
            retry_after=retry_after,
        )


def service_url(address: str, schemes: frozenset[str]) -> str:
    """Get the URL of a service from the address of its service info.

    Args:
        address: Address of the service info.
        schemes: Schemes allowed in service URLs.

    Returns:
        The address without `SERVICE_INFO_PATH`.

    Raises:
        InvalidService: The address is invalid, uses a scheme that isn't
            allowed, has a query or fragment, or doesn't end in
            `SERVICE_INFO_PATH`.
    """
    check_url_scheme(address, schemes=schemes)
    if "?" in address or "#" in address or not address.endswith(SERVICE_INFO_PATH):
        raise InvalidService(
            f"The address of the service info must end in '{SERVICE_INFO_PATH}'."
        )
    return address.removesuffix(SERVICE_INFO_PATH)


def private_networks() -> list[IPNetwork]:
    """Get the networks that fetches may reach besides public addresses.

    Must be called in an app context.

    Returns:
        Networks (`custom.endpoints.services.service_info.private_networks`).
    """
    services = get_custom_config(CustomConfig).endpoints.services
    return [
        ipaddress.ip_network(item) for item in services.service_info.private_networks
    ]


def fetch_service_info(address: str) -> object:
    """Fetch service info (cf. `cloud_registry.safe_fetch.safe_get()`).

    Must be called in an app context.

    Args:
        address: Address of the service info, checked by `service_url()` or
            built from a stored service URL.

    Returns:
        The decoded JSON document, of any type.

    Raises:
        cloud_registry.safe_fetch.AddressNotAllowed: See `safe_get()`.
        cloud_registry.safe_fetch.Unreachable: See `safe_get()`.
        InvalidServiceInfo: The response isn't JSON.
    """
    services = get_custom_config(CustomConfig).endpoints.services
    body = safe_get(
        # with an encoded host name
        normalize_url(address),
        schemes=frozenset(services.url_schemes),
        networks=private_networks(),
        headers=FETCH_HEADERS,
    )
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise InvalidServiceInfo("The response is not JSON.") from exc


@cache
def _registration_schema() -> Draft4Validator:
    """Get the validator of registrations of the API specification.

    Returns:
        Validator of the schema `ExternalServiceRegister`, as Connexion checks
        `POST /services`, so that the limits are defined once.
    """
    schemas = yaml.safe_load(SPECIFICATION.read_text())["components"]["schemas"]
    return Draft4Validator(
        {
            **schemas["ExternalServiceRegister"],
            "components": {
                "schemas": {"ServiceTypeRegister": schemas["ServiceTypeRegister"]}
            },
        }
    )


def _check_schema(values: dict[str, Any]) -> None:
    """Check values for a registration against the schema of registrations.

    Args:
        values: Values for a registration.

    Raises:
        InvalidServiceInfo: A field is missing or invalid; the message names
            the first one, but not its value, which comes from elsewhere.
    """
    for error in sorted(
        _registration_schema().iter_errors(values), key=lambda item: list(item.path)
    ):
        path = [str(part) for part in error.path]
        if error.validator == "required":
            missing = set(error.validator_value) - set(error.instance)
            path.append(min(missing))
        raise InvalidServiceInfo(f"'{'.'.join(path)}' is missing or invalid.")


def service_values(service_info: object, url: str) -> dict[str, Any]:
    """Get the values for a registration from service info.

    Args:
        service_info: Decoded service info.
        url: URL of the service.

    Returns:
        The fields of the service info that a registration can have (cf.
        `SERVICE_FIELDS`, `OBJECT_FIELDS`), and the URL. Fields set to `null`
        count as missing.

    Raises:
        InvalidServiceInfo: The service info is no object, has no identifier
            (`id`), which all service info has and identifies the service for
            syncs, or doesn't pass the checks of a registration.
    """
    if not isinstance(service_info, dict):
        raise InvalidServiceInfo("The response is not a JSON object.")
    identifier = service_info.get("id")
    if not isinstance(identifier, str) or not 0 < len(identifier) <= MAX_ID_LENGTH:
        raise InvalidServiceInfo("'id' is missing or invalid.")
    values: dict[str, Any] = {
        field: service_info[field]
        for field in SERVICE_FIELDS
        if service_info.get(field) is not None
    }
    for field, keys in OBJECT_FIELDS.items():
        item = service_info.get(field)
        if isinstance(item, dict):
            values[field] = {
                key: item[key] for key in keys if item.get(key) is not None
            }
        elif item is not None:
            values[field] = item
    values["url"] = url
    _check_schema(values)
    try:
        RegisterService(data=values).check()
    except InvalidService as exc:
        raise InvalidServiceInfo(str(exc.description)) from exc
    return values


def check_fetch_limit(user: str) -> None:
    """Count a fetch of a user, unless the user is at the limit.

    Each fetch takes a single atomic write, also for concurrent requests.

    Args:
        user: User identifier.

    Raises:
        FetchLimit: The user started as many fetches in the current hour as
            allowed (`custom.endpoints.services.service_info.fetches_per_hour`).
    """
    services = get_custom_config(CustomConfig).endpoints.services
    limit = services.service_info.fetches_per_hour
    fetches = get_collection(FETCHES)
    current = now()
    window = current - FETCH_WINDOW
    if fetches.update_one(
        {"_id": user, "windowStart": {"$gt": window}, "count": {"$lt": limit}},
        {"$inc": {"count": 1}},
    ).modified_count:
        return
    try:
        # a new window, unless the user has one, at the limit (duplicate key)
        fetches.update_one(
            {"_id": user, "windowStart": {"$lte": window}},
            {"$set": {"windowStart": current, "count": 1}},
            upsert=True,
        )
    except DuplicateKeyError as exc:
        stored = fetches.find_one({"_id": user}) or {}
        wait = FETCH_WINDOW
        if "windowStart" in stored:
            # naive, as read from the database
            wait -= current.replace(tzinfo=None) - stored["windowStart"].replace(
                tzinfo=None
            )
        raise FetchLimit(limit, retry_after=max(int(wait.total_seconds()), 1)) from exc


# POST /services/import
def postServiceImport(service_import: dict[str, Any], **kwargs: Any) -> Any:
    """Get the values for a registration from the service info of a service.

    Args:
        service_import: Request body, with the address of the service info as
            `serviceInfoUrl`.
        **kwargs: Further arguments that Connexion passes (`user`,
            `token_info`); unused.

    Returns:
        Values for a registration (cf. `service_values()`); or an error
        response: `400` for addresses that aren't one of service info, or
        service info that doesn't pass the checks of a registration, `429`
        above the limit of the user (cf. `check_fetch_limit()`), and `502` if
        the fetch fails, for any reason.
    """
    address: str = service_import["serviceInfoUrl"]
    services = get_custom_config(CustomConfig).endpoints.services
    try:
        url = service_url(address, schemes=frozenset(services.url_schemes))
        check_fetch_limit(require_subject())
        return service_values(fetch_service_info(address), url)
    except FetchError as exc:
        logger.info(
            "Service info not imported by '%s' from '%s': %s.",
            current_user(),
            escape_log_value(address),
            type(exc).__name__,
        )
        return problem(FetchFailed())
    except FetchLimit as exc:
        logger.info("Service info not imported: '%s' is at the limit.", current_user())
        return problem(exc)
    except InvalidService as exc:
        logger.info(
            "Service info not imported by '%s' from '%s': %s",
            current_user(),
            escape_log_value(address),
            escape_log_value(exc.description),
        )
        return problem(exc)
