"""Controller for service info endpoint."""

import logging
from datetime import datetime, timezone
from typing import Dict, FrozenSet, Optional

from flask import current_app

from cloud_registry.exceptions import NotFound

logger = logging.getLogger(__name__)
TIMESTAMP_FIELDS = frozenset({"createdAt", "updatedAt"})


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
            api_path: Base path at which API endpoints can be reached for this
                application instance.
            conf_info: Service info details as per endpoints config.
            collection: Database collection storing service info objects.
        """
        foca_conf = current_app.config.foca  # type: ignore[attr-defined]
        endpoint_conf = foca_conf.custom.endpoints
        self.url_prefix = endpoint_conf.service.url_prefix
        self.host_name = endpoint_conf.service.external_host
        self.external_port = endpoint_conf.service.external_port
        self.api_path = endpoint_conf.service.api_path
        self.conf_info = endpoint_conf.service_info.dict(exclude_none=True)
        self.collection = (
            foca_conf.db.dbs["serviceStore"].collections["service_info"].client
        )

    def get_service_info(self) -> Dict:
        """Get latest service info from database.

        Returns:
            Latest service info details.
        """
        try:
            return (
                self.collection.find({}, {"_id": False})
                .sort([("_id", -1)])
                .limit(1)
                .next()
            )
        except StopIteration:
            raise NotFound

    def set_service_info_from_config(
        self,
    ) -> None:
        """Create or update service info from service configuration.

        Will create service info if it does not exist or current
        configuration differs from available one.

        Raises:
            cloud_registry.exceptions.ValidationError: Service info
                configuration does not conform to API specification.
        """
        try:
            db_info = self.get_service_info()
        except NotFound:
            db_info = {}
        service_info = self._get_service_info_from_config(
            db_info=db_info or None,
        )
        ignored_fields = self._dynamic_timestamp_fields()
        if (
            db_info
            and self._without_fields(db_info, ignored_fields)
            == self._without_fields(service_info, ignored_fields)
            and self._has_fields(db_info, ignored_fields)
        ):
            logger.info("Using available service info.")
            return
        self._upsert_service_info(data=service_info)
        logger.info("Service info registered.")

    def set_service_info_from_app_context(
        self,
        data: Dict,
    ) -> Dict:
        """Return service info.

        Arguments:
            data: Service info according to API specification.

        Returns:
            Response headers.
        """
        self._upsert_service_info(data=data)
        return self._get_headers()

    def _upsert_service_info(
        self,
        data: Dict,
    ) -> None:
        """Insert or updated service info document."""
        self.collection.replace_one(
            filter={"id": data["id"]},
            replacement=data,
            upsert=True,
        )

    def _get_headers(self) -> Dict:
        """Build dictionary of response headers.

        Returns:
            Response headers.
        """
        headers: Dict = {
            "Content-type": "application/json",
        }
        headers["Location"] = (
            f"{self.url_prefix}://{self.host_name}:{self.external_port}/"
            f"{self.api_path}/service-info"
        )
        return headers

    def _get_service_info_from_config(
        self,
        db_info: Optional[Dict] = None,
    ) -> Dict:
        """Build service info from config and dynamic timestamp fields."""
        service_info = dict(self.conf_info)
        current_timestamp = self._current_timestamp()
        if not service_info.get("createdAt"):
            service_info["createdAt"] = (
                current_timestamp
                if db_info is None or not db_info.get("createdAt")
                else db_info["createdAt"]
            )
        if not service_info.get("updatedAt"):
            service_info["updatedAt"] = current_timestamp
        return service_info

    @staticmethod
    def _current_timestamp() -> str:
        """Return the current UTC timestamp in RFC 3339 format."""
        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _dynamic_timestamp_fields(self) -> FrozenSet[str]:
        """Return timestamp fields that should be auto-managed."""
        return frozenset(
            field for field in TIMESTAMP_FIELDS if not self.conf_info.get(field)
        )

    @staticmethod
    def _has_fields(data: Dict, fields: FrozenSet[str]) -> bool:
        """Check whether all requested fields are present in service info."""
        return all(data.get(field) for field in fields)

    @staticmethod
    def _without_fields(data: Dict, fields: FrozenSet[str]) -> Dict:
        """Remove a selected set of fields from service-info data."""
        return {key: value for key, value in data.items() if key not in fields}
