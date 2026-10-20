"""Typed access to the configuration and collections of the app."""

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
        raise RuntimeError(f"No custom configuration of type {CustomConfig}")
    return custom


def get_collection(name: str) -> Collection:
    """Get a collection of the service store of the current app.

    Args:
        name: Name of the collection, as configured.

    Returns:
        MongoDB collection.
    """
    return get_foca_collection(db=DB, collection=name)
