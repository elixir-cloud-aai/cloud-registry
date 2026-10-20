"""Typed access to the configuration, collections and public URLs of the
app.
"""

from urllib.parse import quote

from flask import request
from foca import get_collection as get_foca_collection
from foca import get_config
from pymongo.collection import Collection

from cloud_registry.service_models.custom_config import CustomConfig

DB = "serviceStore"


def get_custom_config() -> CustomConfig:
    """Get the custom configuration of the current app.

    Returns:
        Custom configuration.

    Raises:
        RuntimeError: The app has no custom configuration.
    """
    # FOCA keeps the custom configuration as an untyped extra field; it is an
    # instance of `CustomConfig` only if the app loads the model by its full
    # path (`cloud_registry.service_models.custom_config.CustomConfig`)
    custom = (get_config().model_extra or {}).get("custom")
    if not isinstance(custom, CustomConfig):
        raise RuntimeError(
            f"No custom configuration of type {CustomConfig}; check that "
            "`custom_config_model` is its full module path"
        )
    return custom


def get_public_url(path: str) -> str:
    """Get the public URL of a path of the current app.

    Must be called in a request context. The URL is built from
    `custom.endpoints.service`, with the path prefix `api_path` (e.g., of a
    reverse proxy) in front of the root path of the app and the path.

    Args:
        path: Path below the root path of the app (e.g., the request path),
            not percent-encoded.

    Returns:
        Public URL, with the root path of the app and the path percent-encoded.
    """
    conf = get_custom_config().endpoints.service
    # `api_path` is configured as it appears in URLs, so it isn't encoded
    parts = (conf.api_path, quote(request.script_root + path))
    url_path = "/".join(part.strip("/") for part in parts if part.strip("/"))
    return f"{conf.url_prefix}://{conf.external_host}:{conf.external_port}/{url_path}"


def get_collection(name: str) -> Collection:
    """Get a collection of the service store of the current app.

    Args:
        name: Name of the collection, as configured.

    Returns:
        MongoDB collection.
    """
    return get_foca_collection(db=DB, collection=name)
