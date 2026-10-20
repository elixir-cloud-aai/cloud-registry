"""Test cases for service registration."""

from copy import deepcopy
import string  # noqa: F401
from unittest.mock import MagicMock, patch

from pymongo.errors import DuplicateKeyError
import pytest

from cloud_registry.exceptions import InternalServerError
from cloud_registry.ga4gh.registry.service import RegisterService
from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import (
    CUSTOM_CONFIG,
    MOCK_ID,
    MOCK_ID_ONE_CHAR,
    MOCK_SERVICE,
)


class TestRegisterService:
    """Tests for `RegisterService` class."""

    def test_init(self, foca_app, service_payload):
        """Test for constructing class."""
        with foca_app.app_context():
            obj = RegisterService(data=service_payload)
            assert obj.data["name"] == service_payload["name"]
            assert obj.data["id"] is None
            assert not obj.replace
            # Caller dict is not modified
            assert "id" not in service_payload

    def test_register_metadata(self, foca_app, service_payload):
        """Test for registering a service with a randomly assigned identifier."""
        with foca_app.app_context():
            obj = RegisterService(data=service_payload)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)
            assert "id" not in service_payload
            assert "_id" not in service_payload

    def test_register_metadata_literal_id_charset(self, foca_app, service_payload):
        """Test for registering a service with a randomly assigned identifier
        generated from a literal character set.
        """
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config["endpoints"]["services"]["id"]["charset"] = MOCK_ID_ONE_CHAR
        custom_config["endpoints"]["services"]["id"]["length"] = 1
        foca_app.config.foca.custom = CustomConfig(**custom_config)

        with foca_app.app_context():
            obj = RegisterService(data=service_payload)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)
            assert obj.data["id"] == MOCK_ID_ONE_CHAR
            assert "id" not in service_payload

    def test_register_metadata_with_id(self, foca_app, service_payload):
        """Test for registering a service with a user-supplied identifier."""
        with foca_app.app_context():
            obj = RegisterService(data=service_payload, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data["id"] == MOCK_ID
            assert obj.replace
            assert not obj.was_replaced
            assert "id" not in service_payload

    def test_register_metadata_with_id_replace(self, foca_app, service_payload):
        """Test for updating an existing obj."""
        coll = (
            foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = MOCK_ID
        coll.insert_one(mock_resp)

        with foca_app.app_context():
            obj = RegisterService(data=service_payload, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data["id"] == MOCK_ID
            assert obj.was_replaced
            assert "id" not in service_payload

    def test_register_metadata_duplicate_key(self, foca_app, service_payload):
        """Test for registering a service; duplicate key error occurs and retries."""
        mock_resp = MagicMock(side_effect=[DuplicateKeyError(""), None])
        foca_app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()
        foca_app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one = mock_resp

        with foca_app.app_context():
            obj = RegisterService(data=service_payload)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)
            assert "id" not in service_payload

    def test_register_metadata_collision_retry_success(self, foca_app, service_payload):
        """Test ID collision retry generates a new ID and succeeds (#72)."""
        coll = (
            foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client
        )
        # Pre-insert a colliding ID into the collection with unique index
        coll.insert_one({"id": "COLLIDE", "name": "Existing Service"})

        data = deepcopy(service_payload)
        with foca_app.app_context():
            with patch(
                "cloud_registry.ga4gh.registry.service.generate_id",
                side_effect=["COLLIDE", "NEW_UNIQUE_ID"],
            ):
                obj = RegisterService(data=data)
                obj.register_metadata()
                assert obj.data["id"] == "NEW_UNIQUE_ID"
                assert "id" not in data
                assert "_id" not in data

    def test_register_metadata_duplicate_keys_repeated(self, foca_app, service_payload):
        """Test registering service when all generated IDs collide (#71, #72)."""
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config["endpoints"]["services"]["id"]["charset"] = MOCK_ID_ONE_CHAR
        custom_config["endpoints"]["services"]["id"]["length"] = 1
        foca_app.config.foca.custom = CustomConfig(**custom_config)

        coll = (
            foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client
        )
        # Pre-insert the only possible 1-character identifier "A" into the collection
        coll.insert_one({"id": MOCK_ID_ONE_CHAR, "name": "Pre-existing"})

        data = deepcopy(service_payload)
        with foca_app.app_context():
            with pytest.raises(InternalServerError):
                obj = RegisterService(data=data)
                obj.register_metadata(retries=3)
            # Caller dict remains unmodified
            assert "id" not in data
            assert "_id" not in data
