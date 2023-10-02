"""Controller for service info endpoint."""

import logging
from typing import Dict

from flask import current_app

from cloud_registry.exceptions import NotFound

logger = logging.getLogger(__name__)


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
        self.conf_info = endpoint_conf.service_info.dict()
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
        add = False
        try:
            db_info = self.get_service_info()
        except NotFound:
            db_info = {}
        add = False if db_info == self.conf_info else True
        if add:
            self._upsert_service_info(data=self.conf_info)
            logger.info("Service info registered.")
        else:
            logger.info("Using available service info.")

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
