"""Tests for getting/setting service info."""

from copy import deepcopy
import string  # noqa: F401

import pytest

from cloud_registry.exceptions import NotFound
from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo
from tests.mock_data import (
    DB,
    HEADERS_SERVICE_INFO,
    SERVICE_CONFIG,
    SERVICE_INFO_CONFIG,
)

coll = "service_info"


class TestRegisterServiceInfo:
    """Tests for `RegisterServiceInfo` class."""

    def test_init(self, foca_app):
        """Test for constructing class."""
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            assert service_info.url_prefix == SERVICE_CONFIG["url_prefix"]

    def test_get_service_info(self, foca_app, service_info_payload):
        """Test for getting service info."""
        collection = foca_app.config.foca.db.dbs[DB].collections[coll].client
        collection.insert_one(deepcopy(service_info_payload))

        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            res = service_info.get_service_info()
            assert res == SERVICE_INFO_CONFIG

    def test_get_service_info_na(self, foca_app):
        """Test for getting service info when service info is unavailable."""
        with foca_app.app_context():
            with pytest.raises(NotFound):
                RegisterServiceInfo().get_service_info()

    def test_set_service_info_from_config(self, foca_app):
        """Test for setting service info from config."""
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            service_info.set_service_info_from_config()
            assert service_info.get_service_info() == SERVICE_INFO_CONFIG

    def test_set_service_info_from_config_skip(self, foca_app, service_info_payload):
        """Test for skipping setting service info because identical service
        info is already available.
        """
        collection = foca_app.config.foca.db.dbs[DB].collections[coll].client
        collection.insert_one(deepcopy(service_info_payload))

        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            service_info.set_service_info_from_config()
            assert service_info.get_service_info() == SERVICE_INFO_CONFIG

    def test_set_service_info_from_app_context(self, foca_app, service_info_payload):
        """Test for setting service info from app context."""
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            service_info.set_service_info_from_app_context(
                data=service_info_payload,
            )
            assert service_info.get_service_info() == SERVICE_INFO_CONFIG

    def test__upsert_service_info_insert(self, foca_app, service_info_payload):
        """Test for creating service info document in database."""
        data = deepcopy(service_info_payload)
        del data["contactUrl"]
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            service_info._upsert_service_info(data=data)
            assert service_info.get_service_info() == data
            assert service_info.get_service_info() != SERVICE_INFO_CONFIG

    def test__upsert_service_info_update(self, foca_app, service_info_payload):
        """Test for replacing service info document in database."""
        collection = foca_app.config.foca.db.dbs[DB].collections[coll].client
        collection.insert_one(deepcopy(service_info_payload))

        data = deepcopy(service_info_payload)
        del data["contactUrl"]
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            service_info._upsert_service_info(data=data)
            assert service_info.get_service_info() == data
            assert service_info.get_service_info() != SERVICE_INFO_CONFIG

    def test__get_headers(self, foca_app):
        """Test for response headers getter."""
        with foca_app.app_context():
            service_info = RegisterServiceInfo()
            headers = service_info._get_headers()
            assert headers == HEADERS_SERVICE_INFO
