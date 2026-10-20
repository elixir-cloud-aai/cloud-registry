"""Test cases for service registration."""

from copy import deepcopy

import pytest

from cloud_registry.exceptions import InternalServerError
from cloud_registry.ga4gh.registry.service import RegisterService
from tests.mock_data import (
    MOCK_ID,
    MOCK_ID_ONE_CHAR,
    MOCK_SERVICE,
)


def _without_db_id(doc):
    del doc["_id"]
    return doc


class TestRegisterService:
    """Tests for `RegisterService` class."""

    @pytest.mark.parametrize("service_id, replace", [(None, False), (MOCK_ID, True)])
    def test_init(self, unit_app, service_id, replace):
        """Test for constructing class, with and without an identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=service_id)
            assert obj.data["name"] == MOCK_SERVICE["name"]
            assert obj.data["id"] == service_id
            assert obj.replace is replace

    def test_register_metadata(self, unit_app, services):
        """Test for registering a service with a randomly assigned identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert isinstance(obj.data["id"], str)
        stored = _without_db_id(services.find_one({"id": obj.data["id"]}))
        assert stored == {**MOCK_SERVICE, "id": obj.data["id"]}

    def test_register_metadata_literal_id_charset(self, unit_app):
        """Test for registering a service with a randomly assigned identifier
        generated from a literal character set.
        """
        id_conf = unit_app.config.foca.custom.endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID_ONE_CHAR

    def test_register_metadata_with_id(self, unit_app, services):
        """Test for registering a service with a user-supplied identifier."""
        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE), id=MOCK_ID)
            obj.register_metadata()
        assert obj.data["id"] == MOCK_ID
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == {**MOCK_SERVICE, "id": MOCK_ID}
        assert services.count_documents({}) == 1

    def test_register_metadata_with_id_replace(self, unit_app, services):
        """Test for updating an existing service."""
        services.insert_one({**MOCK_SERVICE, "id": MOCK_ID})

        data = {**MOCK_SERVICE, "name": "new name"}
        with unit_app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
        stored = _without_db_id(services.find_one({"id": MOCK_ID}))
        assert stored == {**data, "id": MOCK_ID}
        assert services.count_documents({}) == 1

    def test_register_metadata_duplicate_key_new_id(
        self, monkeypatch, unit_app, services
    ):
        """Test for registering a service; a new identifier is generated after
        a duplicate key error.
        """
        services.insert_one({"id": "AAAAAA"})
        ids = iter(["AAAAAA", "BBBBBB"])
        monkeypatch.setattr(
            "cloud_registry.ga4gh.registry.service.generate_id",
            lambda **kwargs: next(ids),
        )

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == "BBBBBB"
        assert services.find_one({"id": "BBBBBB"})["name"] == MOCK_SERVICE["name"]

    def test_register_metadata_duplicate_keys_repeated(self, unit_app, services):
        """Test for registering a service; running out of unique identifiers."""
        id_conf = unit_app.config.foca.custom.endpoints.services.id
        id_conf.charset = MOCK_ID_ONE_CHAR
        id_conf.length = 1
        services.insert_one({"id": MOCK_ID_ONE_CHAR})

        with unit_app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            with pytest.raises(InternalServerError):
                obj.register_metadata()
        assert services.count_documents({}) == 1

    def test_register_metadata_does_not_modify_data(self, unit_app):
        """Test that registering a service does not modify the caller's data."""
        data = deepcopy(MOCK_SERVICE)

        with unit_app.app_context():
            RegisterService(data=data).register_metadata()
            RegisterService(data=data, id=MOCK_ID).register_metadata()
        assert data == MOCK_SERVICE
