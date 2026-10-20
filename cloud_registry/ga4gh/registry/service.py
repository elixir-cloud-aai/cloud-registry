"""Controller for registering services."""

from copy import deepcopy
import logging

from foca.utils.misc import generate_id
from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import InternalServerError
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)


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
            id_charset: A string of allowed characters for generating service
                identifiers.
            id_length: Length of generated service identifiers.
            db_coll: Database collection for storing service objects.
        """
        endpoint_conf = get_custom_config().endpoints
        self.data = deepcopy(data)
        self.data["id"] = id
        self.replace = id is not None
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
        """
        # replace or insert service with provided ID (PUT)
        if self.replace:
            self.db_coll.replace_one(
                filter={"id": self.data["id"]},
                replacement=self.data,
                upsert=True,
            )

        # insert service with random ID (POST); new ID if key exists
        else:
            for _ in range(retries + 1):
                self.data["id"] = generate_id(
                    charset=self.id_charset, length=self.id_length
                )
                try:
                    # insert a copy, as `insert_one()` adds an `_id` field
                    self.db_coll.insert_one(document=dict(self.data))
                except DuplicateKeyError:
                    continue
                logger.info("Added service with id '%s'.", self.data["id"])
                break
            else:
                raise InternalServerError
        logger.debug(
            "Entry in 'services' collection: %s",
            self.db_coll.find_one({"id": self.data["id"]}),
        )
