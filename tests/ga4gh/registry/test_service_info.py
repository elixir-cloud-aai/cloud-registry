"""Tests for getting/setting service info."""

from copy import deepcopy
import re
import pytest

from tests.mock_data import (
    HEADERS_SERVICE_INFO,
    SERVICE_CONFIG,
    SERVICE_INFO_CONFIG,
)
from cloud_registry.ga4gh.registry.service_info import (
    RegisterServiceInfo,
)
from cloud_registry.exceptions import NotFound


class TestRegisterServiceInfo:
    """Tests for `RegisterServiceInfo` class."""

    def test_init(self, unit_app):
        """Test for constructing class."""
        with unit_app.app_context():
            service_info = RegisterServiceInfo()
            assert service_info.url_prefix == SERVICE_CONFIG["url_prefix"]

    def test_get_service_info(self, unit_app, service_info):
        """Test for getting service info."""
        service_info.insert_one(deepcopy(SERVICE_INFO_CONFIG))

        with unit_app.app_context():
            res = RegisterServiceInfo().get_service_info()
            assert res == SERVICE_INFO_CONFIG

    def test_get_service_info_na(self, unit_app):
        """Test for getting service info when service info is unavailable."""
        with unit_app.app_context():
            with pytest.raises(NotFound):
                RegisterServiceInfo().get_service_info()

    def test_set_service_info_from_config(self, unit_app):
        """Test for setting service info from config."""
        with unit_app.app_context():
            service_info = RegisterServiceInfo()
            service_info.set_service_info_from_config()
            assert service_info.get_service_info() == SERVICE_INFO_CONFIG

    def test_set_service_info_from_config_skip(self, unit_app, service_info):
        """Test for skipping setting service info because identical service
        info is already available.
        """
        service_info.insert_one(deepcopy(SERVICE_INFO_CONFIG))

        with unit_app.app_context():
            register = RegisterServiceInfo()
            register.set_service_info_from_config()
            assert register.get_service_info() == SERVICE_INFO_CONFIG
        assert service_info.count_documents({}) == 1

    def test_set_service_info_from_app_context(self, unit_app):
        """Test for setting service info from app context."""
        with unit_app.app_context():
            service_info = RegisterServiceInfo()
            service_info.set_service_info_from_app_context(
                data=SERVICE_INFO_CONFIG,
            )
            assert service_info.get_service_info() == SERVICE_INFO_CONFIG

    def test__upsert_service_info_insert(self, unit_app):
        """Test for creating service info document in database."""
        data = deepcopy(SERVICE_INFO_CONFIG)
        del data["contactUrl"]
        with unit_app.app_context():
            service_info = RegisterServiceInfo()
            service_info._upsert_service_info(data=data)
            assert service_info.get_service_info() == data
            assert service_info.get_service_info() != SERVICE_INFO_CONFIG

    def test__upsert_service_info_update(self, unit_app, service_info):
        """Test for replacing service info document in database."""
        service_info.insert_one(deepcopy(SERVICE_INFO_CONFIG))

        data = deepcopy(SERVICE_INFO_CONFIG)
        del data["contactUrl"]
        with unit_app.app_context():
            register = RegisterServiceInfo()
            register._upsert_service_info(data=data)
            assert register.get_service_info() == data
            assert register.get_service_info() != SERVICE_INFO_CONFIG
        assert service_info.count_documents({}) == 1

    def test__get_headers(self, unit_app):
        """Test for response headers getter."""
        with unit_app.app_context():
            headers = RegisterServiceInfo()._get_headers()
            assert headers == HEADERS_SERVICE_INFO


class TestServiceInfoTimestamps:
    """Tests for timestamps of service info registered from a config without
    `createdAt` and `updatedAt`.
    """

    @pytest.fixture
    def app(self, unit_app, monkeypatch):
        """App whose service info config has no timestamps, with a fake clock."""
        conf = unit_app.config.foca.custom.endpoints.service_info
        conf.createdAt = None
        conf.updatedAt = None
        self.now = "2026-01-01T00:00:00Z"
        monkeypatch.setattr(
            RegisterServiceInfo, "_current_timestamp", staticmethod(lambda: self.now)
        )
        return unit_app

    def _register(self, app):
        with app.app_context():
            register = RegisterServiceInfo()
            register.set_service_info_from_config()
            return register.get_service_info()

    def test_first_registration(self, app):
        """Both timestamps are set to the current time."""
        info = self._register(app)
        assert info["createdAt"] == info["updatedAt"] == self.now

    def test_unchanged_config(self, app, service_info):
        """Restarting with an unchanged config keeps the timestamps."""
        first = self._register(app)
        self.now = "2026-02-01T00:00:00Z"
        assert self._register(app) == first
        assert service_info.count_documents({}) == 1

    def test_changed_config(self, app, service_info):
        """A changed config updates `updatedAt` but keeps `createdAt`."""
        self._register(app)
        app.config.foca.custom.endpoints.service_info.description = "new"
        self.now = "2026-02-01T00:00:00Z"
        info = self._register(app)
        assert info["description"] == "new"
        assert info["createdAt"] == "2026-01-01T00:00:00Z"
        assert info["updatedAt"] == self.now
        assert service_info.count_documents({}) == 1

    def test_stored_without_timestamps(self, app, service_info):
        """Service info stored without timestamps gets them."""
        data = deepcopy(SERVICE_INFO_CONFIG)
        del data["createdAt"]
        del data["updatedAt"]
        service_info.insert_one(data)
        info = self._register(app)
        assert info["createdAt"] == info["updatedAt"] == self.now

    def test_current_timestamp(self):
        """The current time is formatted as an RFC 3339 UTC timestamp."""
        timestamp = RegisterServiceInfo._current_timestamp()
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", timestamp)
