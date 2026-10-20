"""Unit tests for endpoint controllers."""

from copy import deepcopy

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
from tests.mock_data import (
    DB,
    MOCK_ID,
    MOCK_SERVICE,
    MOCK_TYPE,
    SERVICE_INFO_CONFIG,
)


# GET /services
def test_getServices(foca_app):
    """Test for getting a list of all available services."""
    coll = foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client
    data = []

    for i in ["serv1", "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        coll.insert_one(mock_resp)

        del mock_resp["_id"]
        data.append(mock_resp)

    with foca_app.app_context():
        res = getServices.__wrapped__()
        assert res == data


# GET /services/{serviceId}
def test_getServiceById(foca_app):
    """Test for getting a service associated with a given identifier."""
    coll = foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client

    for i in [MOCK_ID, "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        coll.insert_one(mock_resp)

    mock_service = deepcopy(MOCK_SERVICE)
    mock_service["id"] = MOCK_ID
    with foca_app.app_context():
        res = getServiceById.__wrapped__(MOCK_ID)
        assert res == mock_service

    with pytest.raises(NotFound):
        with foca_app.app_context():
            getServiceById.__wrapped__("serv4")


# GET /services/types
def test_getServiceTypes_duplicates(foca_app):
    """Test for getting a list of all available service types when only
    services of the same service type are registered.
    """
    coll = foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client

    for i in ["serv1", "serv2", "serv3"]:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        coll.insert_one(mock_resp)

    with foca_app.app_context():
        res = getServiceTypes.__wrapped__()
        assert res == [MOCK_TYPE]


def test_getServiceTypes_distinct(foca_app):
    """Test for getting a list of all available service types when all
    registered services are of distinct service types.
    """
    coll = foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client

    services = ["serv1", "serv2", "serv3"]
    for i in services:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        mock_resp["type"]["artifact"] = i
        coll.insert_one(mock_resp)

    with foca_app.app_context():
        res = getServiceTypes.__wrapped__()
        assert len(res) == len(services)
        assert set([s["artifact"] for s in res]) == set(services)


# GET /service-info
def test_getServiceInfo(foca_app):
    """Test for getting service info."""
    coll = foca_app.config.foca.db.dbs[DB].collections["service_info"].client
    mock_resp = deepcopy(SERVICE_INFO_CONFIG)
    coll.insert_one(mock_resp)

    with foca_app.app_context():
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


# POST /service
def test_postService(foca_app, service_payload):
    """Test for registering a service; identifier assigned by implementation."""
    data = deepcopy(service_payload)
    data["id"] = MOCK_ID
    with foca_app.test_request_context(json=data):
        res = postService.__wrapped__()
        assert isinstance(res, str)


def test_postService_invalid_payload(foca_app):
    """Test for registering a service; identifier assigned by implementation,
    given invalid payload.
    """
    with pytest.raises(BadRequest):
        with foca_app.test_request_context(json=""):
            postService.__wrapped__()


# DELETE /service/{serviceId}
def test_deleteService(foca_app):
    """Test for deleting a service."""
    coll = foca_app.config.foca.db.dbs["serviceStore"].collections["services"].client
    mock_resp = deepcopy(MOCK_SERVICE)
    mock_resp["id"] = MOCK_ID
    coll.insert_one(mock_resp)

    with foca_app.app_context():
        res = deleteService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_deleteService_NotFound(foca_app):
    """Test for deleting a service if a service with the specified identifier
    is not available.
    """
    with foca_app.app_context():
        with pytest.raises(NotFound):
            deleteService.__wrapped__(serviceId="nonexistent")


# PUT /service/{serviceId}
def test_putService(foca_app, service_payload):
    """Test for registering a service; identifier provided by client."""
    with foca_app.test_request_context(json=service_payload):
        res = putService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_putService_invalid_payload(foca_app):
    """Test for registering a service; identifier provided by client, given
    invalid payload.
    """
    with pytest.raises(BadRequest):
        with foca_app.test_request_context(json=""):
            putService.__wrapped__(serviceId=MOCK_ID)


# POST /service-info
def test_postServiceInfo(foca_app, service_info_payload):
    """Test for creating service info."""
    with foca_app.test_request_context(json=service_info_payload):
        postServiceInfo.__wrapped__()
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


def test_postServiceInfo_invalid_payload(foca_app):
    """Test for creating service info, given invalid payload."""
    with pytest.raises(BadRequest):
        with foca_app.test_request_context(json=""):
            postServiceInfo.__wrapped__()
