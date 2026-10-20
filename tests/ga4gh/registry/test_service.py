"""Test cases for service registration."""

from copy import deepcopy
import string  # noqa: F401
from unittest.mock import MagicMock

from flask import Flask
from foca.models.config import Config, MongoConfig
import mongomock
from pymongo.errors import DuplicateKeyError
import pytest

from cloud_registry.exceptions import (
    # BadRequest,
    InternalServerError,
)
from cloud_registry.ga4gh.registry.service import RegisterService
from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import (
    CUSTOM_CONFIG,
    MOCK_ID,
    MOCK_ID_ONE_CHAR,
    MOCK_SERVICE,
    MONGO_CONFIG,
)


def _raise(exception) -> None:
    """General purpose exception raiser."""
    raise exception


class TestRegisterService:
    """Tests for `RegisterService` class."""

    def test_init(self):
        """Test for constructing class."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            assert obj.data["name"] == MOCK_SERVICE["name"]
            assert obj.data["id"] is None

    def test_register_metadata(self):
        """Test for registering a service with a randomly assigned identifier."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)

    def test_register_metadata_literal_id_charset(self):
        """Test for registering a service with a randomly assigned identifier
        generated from a literal character set.
        """
        app = Flask(__name__)
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config["endpoints"]["services"]["id"]["charset"] = MOCK_ID_ONE_CHAR
        custom_config["endpoints"]["services"]["id"]["length"] = 1
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**custom_config),
        )
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)

    def test_register_metadata_with_id(self):
        """Test for registering a service with a user-supplied identifier."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data["id"] == MOCK_ID

    def test_register_metadata_with_id_replace(self):
        """Test for updating an existing obj."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one(mock_resp)

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data["id"] == MOCK_ID

    def test_register_metadata_duplicate_key(self):
        """Test for registering a service; duplicate key error occurs."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        mock_resp = MagicMock(side_effect=[DuplicateKeyError(""), None])
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client = MagicMock()
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one = mock_resp

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data["id"], str)

    def test_register_metadata_duplicate_key_new_id(self, monkeypatch):
        """Test for registering a service; a new identifier is generated after
        a duplicate key error.
        """
        app = _app_with_unique_index()
        coll = app.config.foca.db.dbs["serviceStore"].collections["services"].client
        coll.insert_one({"id": "AAAAAA"})
        ids = iter(["AAAAAA", "BBBBBB"])
        monkeypatch.setattr(
            "cloud_registry.ga4gh.registry.service.generate_id",
            lambda **kwargs: next(ids),
        )

        with app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            obj.register_metadata()
        assert obj.data["id"] == "BBBBBB"
        assert coll.find_one({"id": "BBBBBB"})["name"] == MOCK_SERVICE["name"]

    def test_register_metadata_duplicate_keys_repeated(self):
        """Test for registering a service; running out of unique identifiers."""
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config["endpoints"]["services"]["id"]["charset"] = MOCK_ID_ONE_CHAR
        custom_config["endpoints"]["services"]["id"]["length"] = 1
        app = _app_with_unique_index(custom_config)
        coll = app.config.foca.db.dbs["serviceStore"].collections["services"].client
        coll.insert_one({"id": MOCK_ID_ONE_CHAR})

        with app.app_context():
            obj = RegisterService(data=deepcopy(MOCK_SERVICE))
            with pytest.raises(InternalServerError):
                obj.register_metadata()
        assert coll.count_documents({}) == 1

    def test_register_metadata_does_not_modify_data(self):
        """Test that registering a service does not modify the caller's data."""
        app = _app_with_unique_index()
        data = deepcopy(MOCK_SERVICE)

        with app.app_context():
            RegisterService(data=data).register_metadata()
            RegisterService(data=data, id=MOCK_ID).register_metadata()
        assert data == MOCK_SERVICE


def _app_with_unique_index(custom_config=CUSTOM_CONFIG) -> Flask:
    """Create app whose services collection is an in-memory MongoDB
    collection with a unique index on `id`.
    """
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**custom_config),
    )
    coll = mongomock.MongoClient().db.services
    coll.create_index("id", unique=True)
    app.config.foca.db.dbs["serviceStore"].collections["services"].client = coll
    return app
