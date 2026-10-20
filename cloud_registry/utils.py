"""Typed access to the configuration and collections of the app."""

from typing import cast

from foca import get_collection as get_foca_collection
from foca import get_config
from pymongo.collection import Collection

from cloud_registry.service_models.custom_config import CustomConfig

DB = "serviceStore"


def get_custom_config() -> CustomConfig:
    """Get the custom configuration of the current app.

    Returns:
        Custom configuration.
    """
    # FOCA keeps the custom configuration as an untyped extra field, parsed
    # with `CustomConfig` (imported by FOCA as `service_models.custom_config`,
    # so `isinstance()` does not apply)
    return cast(CustomConfig, (get_config().model_extra or {})["custom"])


def get_collection(name: str) -> Collection:
    """Get a collection of the service store of the current app.

    Args:
        name: Name of the collection, as configured.

    Returns:
        MongoDB collection.
    """
    return get_foca_collection(db=DB, collection=name)
