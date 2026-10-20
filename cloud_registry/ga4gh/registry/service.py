"""Controller for registering services."""

from copy import deepcopy
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
            data: Service metadata; a copy of `data`, with the identifier.
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
        self.data = deepcopy(data)
        self.data["id"] = id
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
        # replace or insert service with provided ID (PUT)
        if self.replace:
            result_object = self.db_coll.replace_one(
                filter={"id": self.data["id"]},
                replacement=self.data,
                upsert=True,
            )
            if result_object.modified_count:
                self.was_replaced = True

        # insert service with random ID (POST); new ID if key exists
        else:
            for i in range(retries + 1):
                self.data["id"] = generate_id(
                    charset=self.id_charset, length=self.id_length
                )
                try:
                    # insert a copy, as `insert_one()` adds an `_id` field
                    self.db_coll.insert_one(document=dict(self.data))
                except DuplicateKeyError:
                    continue
                logger.info(f"Added service with id '{self.data['id']}'.")
                break
            else:
                raise InternalServerError
        logger.debug(
            "Entry in 'services' collection: "
            f"{self.db_coll.find_one({'id': self.data['id']})}"
        )
