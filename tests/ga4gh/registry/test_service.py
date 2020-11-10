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
from tests.mock_data import (
    ENDPOINT_CONFIG,
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
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            assert obj.data['name'] == MOCK_SERVICE['name']
            assert obj.data['id'] is None

    def test_process_metadata(self):
        """Test for processing metadata."""
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.process_metadata()
            assert isinstance(obj.id_charset, str)

    def test_register_metadata(self):
        """Test for registering a service with a randomly assigned identifier.
        """
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
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
        endpoint_config = deepcopy(ENDPOINT_CONFIG)
        endpoint_config['services']['id']['charset'] = MOCK_ID_ONE_CHAR
        endpoint_config['services']['id']['length'] = 1
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=endpoint_config,
        )
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data['id'], str)

    def test_register_metadata_with_id(self):
        """Test for registering a service with a user-supplied identifier."""
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data['id'] == MOCK_ID

    def test_register_metadata_with_id_replace(self):
        """Test for updating an existing obj."""
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = MOCK_ID
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data, id=MOCK_ID)
            obj.register_metadata()
            assert obj.data['id'] == MOCK_ID

    def test_register_metadata_duplicate_key(self):
        """Test for registering a service; duplicate key error occurs."""
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=ENDPOINT_CONFIG,
        )
        mock_resp = MagicMock(side_effect=[DuplicateKeyError(''), None])
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client = MagicMock()
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client.insert_one = mock_resp

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            obj = RegisterService(data=data)
            obj.register_metadata()
            assert isinstance(obj.data['id'], str)

    def test_register_metadata_duplicate_keys_repeated(self):
        """Test for registering a service; running out of unique identifiers.
        """
        endpoint_config = deepcopy(ENDPOINT_CONFIG)
        endpoint_config['services']['id']['length'] = MOCK_ID_ONE_CHAR
        endpoint_config['services']['id']['length'] = 1
        app = Flask(__name__)
        app.config['FOCA'] = Config(
            db=MongoConfig(**MONGO_CONFIG),
            endpoints=endpoint_config,
        )
        mock_resp = deepcopy(MOCK_SERVICE)
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client = mongomock.MongoClient().db.collection
        app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
            .client.insert_one(mock_resp)

        data = deepcopy(MOCK_SERVICE)
        with app.app_context():
            with pytest.raises(InternalServerError):
                obj = RegisterService(data=data)
                obj.register_metadata()
                obj = RegisterService(data=data)
                obj.register_metadata()
                print(obj.data['id'])
