"""Controller for registering services."""

import logging
import string  # noqa: F401
from typing import Dict, Optional

from flask import current_app
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import InternalServerError
from foca.utils.misc import generate_id

logger = logging.getLogger(__name__)


class RegisterService:
    """Class for registering services with the registry."""

    def __init__(
        self,
        data: Dict,
        id: Optional[str] = None,
    ) -> None:
        """Initialize service data.

        Args:
            data: Service metadata consistent with the
            `ExternalServiceRegister` schema.
            id: Service identifier. Auto-generated if not provided.

        Attributes:
            data: Service metadata.
            replace: Whether an existing service with the provided identifier
                should be replaced. Set to `True` if an `id` is provided,
                otherwise set to `False`.
            was_replaced: Whether an existing service with the provided
                identifier was replaced.
            id_charset: A set of allowed characters or an expression evaluating
                to an allowed character set for generating service identifiers.
            id_length: Length of generated service identifiers.
            db_coll: Database collection for storing service objects.
        """
        foca_conf = current_app.config.foca  # type: ignore[attr-defined]
        endpoint_conf = foca_conf.custom.endpoints
        self.data = data.copy()
        self.data["id"] = None if id is None else id
        self.replace = id is not None
        self.was_replaced = False
        self.id_charset: str = endpoint_conf.services.id.charset
        self.id_length = int(endpoint_conf.services.id.length)
        self.db_coll = foca_conf.db.dbs["serviceStore"].collections["services"].client

    def register_metadata(self, retries: int = 9) -> None:
        """Register service.

        Args:
            retries: How many times should the generation of a random
                identifier and insertion into the database be retried when
                encountering `DuplicateKeyError`s if a service identifier was
                not provided.
        """
        # replace or insert service, then return (PUT)
        if self.replace:
            result_object = self.db_coll.replace_one(
                filter={"id": self.data["id"]},
                replacement=self.data,
                upsert=True,
            )
            if result_object.matched_count or result_object.modified_count:
                self.was_replaced = True
            logger.info(f"Replaced service with id '{self.data['id']}'.")
            logger.debug(
                "Entry in 'services' collection: "
                f"{self.db_coll.find_one({'id': self.data['id']})}"
            )
            return

        # keep trying to generate unique ID (POST)
        for i in range(retries + 1):
            self.data["id"] = generate_id(
                charset=self.id_charset, length=self.id_length
            )

            # insert service (POST); continue with next iteration if key exists
            insert_doc = self.data.copy()
            try:
                self.db_coll.insert_one(document=insert_doc)
            except DuplicateKeyError:
                logger.debug(
                    f"Service with id '{self.data['id']}' already exists. "
                    f"Retrying ({i + 1}/{retries + 1})..."
                )
                continue

            logger.info(f"Added service with id '{self.data['id']}'.")
            break
        else:
            logger.error(
                f"Failed to generate a unique service ID after {retries + 1} attempts."
            )
            raise InternalServerError

        logger.debug(
            "Entry in 'services' collection: "
            f"{self.db_coll.find_one({'id': self.data['id']})}"
        )
