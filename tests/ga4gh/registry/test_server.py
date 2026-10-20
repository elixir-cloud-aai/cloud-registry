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
    MOCK_ID,
    MOCK_SERVICE,
    MOCK_TYPE,
    SERVICE_INFO_CONFIG,
)


def _insert_services(services, ids):
    """Insert copies of the mock service with the given identifiers; return
    them without the database's `_id` fields.
    """
    data = []
    for i in ids:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        services.insert_one(mock_resp)
        del mock_resp["_id"]
        data.append(mock_resp)
    return data


# GET /services
def test_getServices(unit_app, services):
    """Test for getting a list of all available services."""
    data = _insert_services(services, ["serv1", "serv2", "serv3"])

    with unit_app.app_context():
        res = getServices.__wrapped__()
        assert res == data


# GET /services/{serviceId}
def test_getServiceById(unit_app, services):
    """Test for getting a service associated with a given identifier."""
    _insert_services(services, [MOCK_ID, "serv2", "serv3"])

    # check whether one service can be retrieved by id
    mock_service = deepcopy(MOCK_SERVICE)
    mock_service["id"] = MOCK_ID
    with unit_app.app_context():
        res = getServiceById.__wrapped__(MOCK_ID)
        assert res == mock_service

    # check whether error is raised if ID does not exist
    with pytest.raises(NotFound):
        with unit_app.app_context():
            res = getServiceById.__wrapped__("serv4")


# GET /services/types
def test_getServiceTypes_duplicates(unit_app, services):
    """Test for getting a list of all available service types when only
    services of the same service type are registered.
    """
    _insert_services(services, ["serv1", "serv2", "serv3"])

    with unit_app.app_context():
        res = getServiceTypes.__wrapped__()
        # All written services have same type, we expect list of length 1
        assert res == [MOCK_TYPE]


# GET /services/types
def test_getServiceTypes_distinct(unit_app, services):
    """Test for getting a list of all available service types when all
    registered services are of distinct service types.
    """
    ids = ["serv3", "serv1", "serv2"]
    for i in ids:
        mock_resp = deepcopy(MOCK_SERVICE)
        mock_resp["id"] = i
        mock_resp["type"]["artifact"] = i
        services.insert_one(mock_resp)

    with unit_app.app_context():
        res = getServiceTypes.__wrapped__()
        # All written services have distinct types, sorted by artifact
        assert res == [{**MOCK_TYPE, "artifact": i} for i in sorted(ids)]


# GET /services/types
def test_getServiceTypes_sort_order(unit_app, services):
    """Service types are sorted by group, then artifact, then version."""
    expected = [
        {"group": "a", "artifact": "b", "version": "2"},
        {"group": "a", "artifact": "c", "version": "1"},
        {"group": "a", "artifact": "c", "version": "2"},
        {"group": "b", "artifact": "a", "version": "1"},
    ]
    for i, service_type in enumerate(reversed(expected * 2)):
        services.insert_one({**MOCK_SERVICE, "id": f"serv{i}", "type": service_type})

    with unit_app.app_context():
        assert getServiceTypes.__wrapped__() == expected


# GET /service-info
def test_getServiceInfo(unit_app, service_info):
    """Test for getting service info."""
    service_info.insert_one(deepcopy(SERVICE_INFO_CONFIG))

    with unit_app.app_context():
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


# POST /service
def test_postService(unit_app, services):
    """Test for registering a service; identifier assigned by implementation."""
    data = deepcopy(MOCK_SERVICE)
    data["id"] = MOCK_ID
    with unit_app.test_request_context(json=data):
        res = postService.__wrapped__()
        assert isinstance(res, str)


def test_postService_invalid_payload(unit_app, services):
    """Test for registering a service; identifier assigned by implementation,
    given invalid payload.
    """
    with pytest.raises(BadRequest):
        with unit_app.test_request_context(json=""):
            postService.__wrapped__()


# DELETE /service/{serviceId}
def test_deleteService(unit_app, services):
    """Test for deleting a service."""
    _insert_services(services, [MOCK_ID])

    with unit_app.app_context():
        res = deleteService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_deleteService_NotFound(unit_app, services):
    """Test for deleting a service if a service with the specified identifier
    is not available.
    """
    services.insert_one(deepcopy(MOCK_SERVICE))

    with unit_app.app_context():
        with pytest.raises(NotFound):
            deleteService.__wrapped__(serviceId=MOCK_ID)


# PUT /service/{serviceId}
def test_putService(unit_app, services):
    """Test for registering a service; identifier provided by client."""
    data = deepcopy(MOCK_SERVICE)
    with unit_app.test_request_context(json=data):
        res = putService.__wrapped__(serviceId=MOCK_ID)
        assert res == MOCK_ID


def test_putService_invalid_payload(unit_app, services):
    """Test for registering a service; identifier provided by client, given
    invalid payload.
    """
    with pytest.raises(BadRequest):
        with unit_app.test_request_context(json=""):
            putService.__wrapped__(serviceId=MOCK_ID)


# POST /service-info
def test_postServiceInfo(unit_app, service_info):
    """Test for creating service info."""
    with unit_app.test_request_context(json=deepcopy(SERVICE_INFO_CONFIG)):
        postServiceInfo.__wrapped__()
        res = getServiceInfo.__wrapped__()
        assert res == SERVICE_INFO_CONFIG


def test_postServiceInfo_invalid_payload(unit_app, service_info):
    """Test for creating service info, given invalid payload."""
    with pytest.raises(BadRequest):
        with unit_app.test_request_context(json=""):
            postServiceInfo.__wrapped__()
