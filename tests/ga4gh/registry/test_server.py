"""Unit tests for endpoint controllers."""

from copy import deepcopy

from flask import Flask
from foca.models.config import (Config, MongoConfig)
import mongomock

from tests.mock_data import (
    DB,
    ENDPOINT_CONFIG,
    MOCK_ID,
    MONGO_CONFIG,
    SERVICE_INFO_CONFIG,
    MOCK_SERVICE,
)
from cloud_registry.ga4gh.registry.server import (
    getServiceById,
    getServiceInfo,
    getServices,
    getServiceTypes,
    postServiceInfo,
)


# GET /services
def test_getServices():
    """Test for getting a list of all available services."""
    app = Flask(__name__)
    app.config['FOCA'] = Config(
        db=MongoConfig(**MONGO_CONFIG)
    )
    mock_resp = deepcopy(MOCK_SERVICE)
    mock_resp['id'] = MOCK_ID
    app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
        .client = mongomock.MongoClient().db.collection
    app.config['FOCA'].db.dbs['serviceStore'].collections['services'] \
        .client.insert_one(mock_resp)

    data = deepcopy(MOCK_SERVICE)
    data['id'] = MOCK_ID
    with app.app_context():
        res = getServices.__wrapped__()
        assert res == [data]


# GET /services/types
def test_getServiceTypes():
    """Test for getting a list of all available service types."""
    app = Flask(__name__)
    app.config['FOCA'] = Config(
        db=MongoConfig(**MONGO_CONFIG)
    )
    with app.app_context():
        res = getServiceTypes.__wrapped__()
        assert res == []


# GET /services/{serviceId}
def test_getServiceById():
    """Test for getting a service associated with a given identifier."""
    app = Flask(__name__)
    app.config['FOCA'] = Config(
        db=MongoConfig(**MONGO_CONFIG)
    )
    with app.app_context():
        res = getServiceById.__wrapped__(MOCK_ID)
        assert res == {}


# GET /service-info
def test_getServiceInfo():
    """Test for getting service info."""
    app = Flask(__name__)
    app.config['FOCA'] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        endpoints=ENDPOINT_CONFIG,
    )
    mock_resp = deepcopy(SERVICE_INFO_CONFIG)
    app.config['FOCA'].db.dbs[DB].collections['service_info'] \
        .client = mongomock.MongoClient().db.collection
    app.config['FOCA'].db.dbs[DB].collections['service_info'] \
        .client.insert_one(mock_resp)

    with app.app_context():
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


# POST /service-info
def test_postServiceInfo():
    """Test for creating service info."""
    app = Flask(__name__)
    app.config['FOCA'] = Config(
        db=MongoConfig(**MONGO_CONFIG),
        endpoints=ENDPOINT_CONFIG,
    )
    app.config['FOCA'].db.dbs[DB].collections['service_info'] \
        .client = mongomock.MongoClient().db.collection

    with app.test_request_context(json=deepcopy(SERVICE_INFO_CONFIG)):
        postServiceInfo.__wrapped__()
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG
