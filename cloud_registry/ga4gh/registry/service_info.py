"""Controller for service info endpoint."""

from datetime import datetime, timezone
import logging

from flask import request

from cloud_registry.exceptions import NotFound
from cloud_registry.utils import get_collection, get_custom_config

logger = logging.getLogger(__name__)
TIMESTAMP_FIELDS = ("createdAt", "updatedAt")
# Internal field with the time a service info document was last written; the
# most recently written service info is served
WRITTEN_AT = "_writtenAt"
PROJECTION = {"_id": False, WRITTEN_AT: False}


class RegisterServiceInfo:
    """Class for registering the service info.

    Creates service info upon first request, if it does not exist.
    """

    def __init__(self) -> None:
        """Initialize class requirements.

        Attributes:
            url_prefix: URL scheme of application instance.
            host_name: Host name of application instance.
            external_port: Port at which application instance is served.
            api_path: Path prefix in front of the API base path in public
                URLs, e.g., when served behind a reverse proxy.
            conf_info: Service info details as per endpoints config.
            collection: Database collection storing service info objects.
        """
        endpoint_conf = get_custom_config().endpoints
        self.url_prefix = endpoint_conf.service.url_prefix
        self.host_name = endpoint_conf.service.external_host
        self.external_port = endpoint_conf.service.external_port
        self.api_path = endpoint_conf.service.api_path
        self.conf_info = endpoint_conf.service_info.model_dump(exclude_none=True)
        self.collection = get_collection("service_info")

    def get_service_info(self) -> dict:
        """Get the most recently written service info from the database.

        Returns:
            Latest service info details.
        """
        try:
            return (
                self.collection.find({}, PROJECTION)
                .sort([(WRITTEN_AT, -1), ("_id", -1)])
                .limit(1)
                .next()
            )
        except StopIteration as exc:
            raise NotFound from exc

    def set_service_info_from_config(
        self,
    ) -> None:
        """Create or update service info from service configuration.

        Will create service info if it does not exist or current
        configuration differs from available one. Service info posted since
        is thus kept on restarts, unless the configuration changes.
        """
        stored = self._get_stored_service_info(id=self.conf_info["id"])
        managed = [field for field in TIMESTAMP_FIELDS if field not in self.conf_info]
        if (
            stored is not None
            and all(stored.get(field) for field in managed)
            and {k: v for k, v in stored.items() if k not in managed} == self.conf_info
        ):
            logger.info("Using available service info.")
            return
        self._upsert_service_info(
            data=self._add_timestamps(data=self.conf_info, stored=stored)
        )
        logger.info("Service info registered.")

    def set_service_info_from_app_context(
        self,
        data: dict,
    ) -> dict:
        """Create or update service info from a request.

        Missing timestamps are added: `createdAt` is kept from stored service
        info with the same identifier (or set to the current time), and
        `updatedAt` is set to the current time.

        Arguments:
            data: Service info according to API specification.

        Returns:
            Response headers.
        """
        stored = self._get_stored_service_info(id=data["id"])
        self._upsert_service_info(data=self._add_timestamps(data=data, stored=stored))
        return self._get_headers()

    def _upsert_service_info(
        self,
        data: dict,
    ) -> None:
        """Insert or update service info document, marking it as the most
        recently written one.
        """
        written_at = datetime.now(timezone.utc).isoformat()
        self.collection.replace_one(
            filter={"id": data["id"]},
            replacement={**data, WRITTEN_AT: written_at},
            upsert=True,
        )

    def _get_headers(self) -> dict:
        """Build dictionary of response headers.

        Returns:
            Response headers, with the public URL of the service info
            endpoint (the current request's path) as `Location`.
        """
        path = "/".join(
            part for part in (self.api_path.strip("/"), request.path.strip("/")) if part
        )
        return {
            "Location": f"{self.url_prefix}://{self.host_name}:{self.external_port}"
            f"/{path}"
        }

    def _get_stored_service_info(self, id: str) -> dict | None:
        """Get stored service info with a given identifier.

        Arguments:
            id: Service info identifier.

        Returns:
            Stored service info, or `None` if there is none.
        """
        return self.collection.find_one({"id": id}, PROJECTION)

    def _add_timestamps(self, data: dict, stored: dict | None) -> dict:
        """Add missing timestamps to service info.

        Arguments:
            data: Service info.
            stored: Stored service info with the same identifier, if any.

        Returns:
            Copy of `data`, with `createdAt` taken from `stored` (or the current
            time) and `updatedAt` set to the current time, unless provided.
        """
        now = self._current_timestamp()
        created_at = (stored or {}).get("createdAt") or now
        return {"createdAt": created_at, "updatedAt": now, **data}

    @staticmethod
    def _current_timestamp() -> str:
        """Return the current UTC timestamp in RFC 3339 format."""
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
