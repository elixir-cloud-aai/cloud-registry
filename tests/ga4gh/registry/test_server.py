"""Unit tests for endpoint controllers."""

from copy import deepcopy

from flask import Flask
from foca.models.config import Config, MongoConfig
import mongomock
import pytest

from cloud_registry.exceptions import BadRequest, NotFound
from cloud_registry.ga4gh.registry.server import (
    deleteService,
    getServiceById,
    getServiceInfo,
    getServices,
    getServiceTypes,
    postService,
    postServiceInfo,
    putService,
)
from cloud_registry.service_models.custom_config import CustomConfig
from tests.mock_data import (
    DB,
    CUSTOM_CONFIG,
    MOCK_ID,
    MOCK_TYPE,
    MONGO_CONFIG,
    SERVICE_INFO_CONFIG,
    MOCK_SERVICE,
)


# GET /services
def test_getServices():
    """Test for getting a list of all available services."""
    app = Flask(__name__)
    app.config.foca = Config(db=MongoConfig(**MONGO_CONFIG))

    data = []

    # write a couple of services into DB
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    for i in ["serv1", "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one(mock_resp)

        # simultaneously save the service entries in a list
        del mock_resp["_id"]
        data.append(mock_resp)

    # check whether getServices returns the same list
    with app.app_context():
        res = getServices.__wrapped__()
        assert res == data


# GET /services/{serviceId}
def test_getServiceById():
    """Test for getting a service associated with a given identifier."""
    app = Flask(__name__)
    app.config.foca = Config(db=MongoConfig(**MONGO_CONFIG))

    # write a couple of services into DB
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    for i in [MOCK_ID, "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one(mock_resp)

    # check whether one service can be retrieved by id
    mock_service = deepcopy(MOCK_SERVICE)
    mock_service["id"] = MOCK_ID
    with app.app_context():
        res = getServiceById.__wrapped__(MOCK_ID)
        assert res == mock_service

    # check whether error is raised if ID does not exist
    with pytest.raises(NotFound):
        with app.app_context():
            res = getServiceById.__wrapped__("serv4")


# GET /services/types
def test_getServiceTypes_duplicates():
    """Test for getting a list of all available service types when only
    services of the same service type are registered.
    """
    app = Flask(__name__)
    app.config.foca = Config(db=MongoConfig(**MONGO_CONFIG))

    # write a couple of services into DB
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    for i in ["serv1", "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one(mock_resp)

    with app.app_context():
        res = getServiceTypes.__wrapped__()
        # All written services have same type, we expect list of length 1
        assert res == [MOCK_TYPE]


# GET /services/types
def test_getServiceTypes_distinct():
    """Test for getting a list of all available service types when all
    registered services are of distinct service types.
    """
    app = Flask(__name__)
    app.config.foca = Config(db=MongoConfig(**MONGO_CONFIG))

    # write a couple of services into DB
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    services = ["serv1", "serv2", "serv3"]
    for i in services:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        mock_resp["type"]["artifact"] = i
        app.config.foca.db.dbs["serviceStore"].collections[
            "services"
        ].client.insert_one(mock_resp)

    with app.app_context():
        res = getServiceTypes.__wrapped__()
        # All written services have distinct types, we expect a list of the
        # same length as there are entries in the database collection
        assert len(res) == len(services)
        # Asserting that artifacts for all services match the ones registered
        # (and are thus distinct)
        assert set([s["artifact"] for s in res]) == set(services)


# GET /service-info
def test_getServiceInfo():
    """Test for getting service info."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    mock_resp = deepcopy(SERVICE_INFO_CONFIG)
    app.config.foca.db.dbs[DB].collections[
        "service_info"
    ].client = mongomock.MongoClient().db.collection
    app.config.foca.db.dbs[DB].collections["service_info"].client.insert_one(mock_resp)

    with app.app_context():
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


# POST /service
def test_postService():
    """Test for registering a service; identifier assigned by implementation."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    data = deepcopy(MOCK_SERVICE)
    data["id"] = MOCK_ID
    with app.test_request_context(json=data):
        res = postService.__wrapped__()
        assert isinstance(res, str)


def test_postService_invalid_payload():
    """Test for registering a service; identifier assigned by implementation,
    given invalid payload.
    """
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    with pytest.raises(BadRequest):
        with app.test_request_context(json=""):
            postService.__wrapped__()


# DELETE /service/{serviceId}
def test_deleteService():
    """Test for deleting a service."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    mock_resp = deepcopy(MOCK_SERVICE)
    mock_resp["id"] = MOCK_ID
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection
    app.config.foca.db.dbs["serviceStore"].collections["services"].client.insert_one(
        mock_resp
    )

    with app.app_context():
        res = deleteService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_deleteService_NotFound():
    """Test for deleting a service if a service with the specified identifier
    is not available.
    """
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    mock_resp = deepcopy(MOCK_SERVICE)
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection
    app.config.foca.db.dbs["serviceStore"].collections["services"].client.insert_one(
        mock_resp
    )

    with app.app_context():
        with pytest.raises(NotFound):
            deleteService.__wrapped__(serviceId=MOCK_ID)


# PUT /service/{serviceId}
def test_putService():
    """Test for registering a service; identifier provided by client."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    data = deepcopy(MOCK_SERVICE)
    with app.test_request_context(json=data):
        res = putService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_putService_invalid_payload():
    """Test for registering a service; identifier provided by client, given
    invalid payload.
    """
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs["serviceStore"].collections[
        "services"
    ].client = mongomock.MongoClient().db.collection

    with pytest.raises(BadRequest):
        with app.test_request_context(json=""):
            putService.__wrapped__(serviceId=MOCK_ID)


# POST /service-info
def test_postServiceInfo():
    """Test for creating service info."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs[DB].collections[
        "service_info"
    ].client = mongomock.MongoClient().db.collection

    with app.test_request_context(json=deepcopy(SERVICE_INFO_CONFIG)):
        postServiceInfo.__wrapped__()
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


def test_postServiceInfo_invalid_payload():
    """Test for creating service info, given invalid payload."""
    app = Flask(__name__)
    app.config.foca = Config(
        db=MongoConfig(**MONGO_CONFIG),
        custom=CustomConfig(**CUSTOM_CONFIG),
    )
    app.config.foca.db.dbs[DB].collections[
        "service_info"
    ].client = mongomock.MongoClient().db.collection

    with pytest.raises(BadRequest):
        with app.test_request_context(json=""):
            postServiceInfo.__wrapped__()
