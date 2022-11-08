"""Test cases for service registration."""

from copy import deepcopy
import string  # noqa: F401
from unittest.mock import MagicMock

from flask import Flask
from foca.models.config import (Config, MongoConfig)
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
            assert obj.data['name'] == MOCK_SERVICE['name']
            assert obj.data['id'] is None

    def test_register_metadata(self):
        """Test for registering a service with a randomly assigned identifier.
        """
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data['id'], str)

    def test_register_metadata_literal_id_charset(self):
        """Test for registering a service with a randomly assigned identifier
        generated from a literal character set.
        """
        app = Flask(__name__)
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config['endpoints']['services']['id']['charset'] = (
            MOCK_ID_ONE_CHAR
        )
        custom_config['endpoints']['services']['id']['length'] = 1
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**custom_config),
        )
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data['id'], str)

    def test_register_metadata_with_id(self):
        """Test for registering a service with a user-supplied identifier."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data['id'] == MOCK_ID

    def test_register_metadata_with_id_replace(self):
        """Test for updating an existing obj."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = MOCK_ID
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data['id'] == MOCK_ID

    def test_register_metadata_duplicate_key(self):
        """Test for registering a service; duplicate key error occurs."""
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        mock_resp = MagicMock(side_effect=[DuplicateKeyError(''), None])
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client.insert_one = mock_resp

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data['id'], str)

    def test_register_metadata_duplicate_keys_repeated(self):
        """Test for registering a service; running out of unique identifiers.
        """
        custom_config = deepcopy(CUSTOM_CONFIG)
        custom_config['endpoints']['services']['id']['length'] = (
            MOCK_ID_ONE_CHAR
        )
        custom_config['endpoints']['services']['id']['length'] = 1
        app = Flask(__name__)
        app.config.foca = Config(
            db=MongoConfig(**MONGO_CONFIG),
            custom=CustomConfig(**CUSTOM_CONFIG),
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client = mongomock.MongoClient().db.collection
        app.config.foca.db.dbs['serviceStore'].collections['services'] \
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            with pytest.raises(InternalServerError):
                obj = RegisterService(data=data)
                obj.register_metadata()
                obj = RegisterService(data=data)
                obj.register_metadata()
                print(obj.data['id'])
