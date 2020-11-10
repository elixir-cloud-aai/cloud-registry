"""Controller for registering services."""

import logging
import string  # noqa: F401
from typing import (Dict, Optional)

from flask import (current_app)
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import InternalServerError
from cloud_registry.utils import generate_id

logger = logging.getLogger(__name__)


class RegisterService:
    """Class to register services with the registry."""

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
        conf = current_app.config['FOCA'].endpoints
        self.data = data
        self.data['id'] = None if id is None else id
        self.replace = True
        self.was_replaced = False
        self.id_charset: str = conf['services']['id']['charset']
        self.id_length = int(conf['services']['id']['length'])
        self.db_coll = (
            current_app.config['FOCA'].db.dbs['serviceStore']
            .collections['services'].client
        )

    def process_metadata(self) -> None:
        """Process service metadata."""
        # evaluate character set expression or interpret literal string as set
        try:
            self.id_charset = eval(self.id_charset)
        except Exception:
            self.id_charset = ''.join(sorted(set(self.id_charset)))

    def register_metadata(self, retries: int = 9) -> None:
        """Register service.

        Args:
            retries: How many times should the generation of a random
                identifier and insertion into the database be retried when
                encountering `DuplicateKeyError`s if a service identifier was
                not provided.
        """
        self.process_metadata()

        # keep trying to generate unique ID
        for i in range(retries + 1):

            # set random ID unless ID is provided
            if self.data['id'] is None:
                self.replace = False
                self.data['id'] = generate_id(
                    charset=self.id_charset,
                    length=self.id_length
                )

            # replace or insert service, then return (PUT)
            if self.replace:
                result_object = self.db_coll.replace_one(
                    filter={'id': self.data['id']},
                    replacement=self.data,
                    upsert=True,
                )
                if result_object.modified_count:
                    self.was_replaced = True
                break

            # insert service (POST); continue with next iteration if key exists
            try:
                self.db_coll.insert_one(document=self.data)
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
