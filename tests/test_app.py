"""App-level test harness exercising endpoints through Connexion and FOCA.

Tests OpenAPI request validation, error formatting, authentication,
endpoint responses, and app startup.
"""

from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import flask_pymongo
from foca.security import auth
import mongomock
import pytest

from cloud_registry.app import main as app_main
from tests.conftest import mock_oidc_get

BASE_URL = "/ga4gh/registry/v1"


class TestAppEndpoints:
    """Test every endpoint through Connexion's test client."""

    # 1. GET /services
    def test_get_services_empty(self, client, auth_headers):
        """Test GET /services with an empty database returns empty list."""
        resp = client.get(f"{BASE_URL}/services", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json == []

    def test_get_services_unauthorized(self, client, auth_headers):
        """Test GET /services without auth token returns 401."""
        resp = client.get(f"{BASE_URL}/services")
        assert resp.status_code == 401
        assert "status" in resp.json
        assert resp.json["status"] == 401

    def test_get_services_invalid_token(self, client):
        """Test GET /services with invalid token returns 401."""
        resp = client.get(
            f"{BASE_URL}/services",
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )
        assert resp.status_code == 401
        assert resp.json["status"] == 401

    # 2. POST /services
    def test_post_service_success(self, client, auth_headers, service_payload):
        """Test POST /services successfully registers a service and returns ID."""
        resp = client.post(
            f"{BASE_URL}/services",
            json=service_payload,
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        service_id = resp.json
        assert isinstance(service_id, str)
        assert len(service_id) > 0

        # Verify service is listed in GET /services
        list_resp = client.get(f"{BASE_URL}/services", headers=auth_headers())
        assert list_resp.status_code == 200
        service_ids = [s["id"] for s in list_resp.json]
        assert service_id in service_ids

    def test_post_service_validation_error(self, client, auth_headers):
        """Test POST /services with invalid body returns 400."""
        invalid_payload = {"name": 12345}  # Missing required fields, invalid type
        resp = client.post(
            f"{BASE_URL}/services",
            json=invalid_payload,
            headers=auth_headers(),
        )
        assert resp.status_code == 400
        assert "status" in resp.json
        assert resp.json["status"] == 400
        assert "title" in resp.json
        assert "detail" in resp.json

    # 3. GET /services/{serviceId}
    def test_get_service_by_id(self, client, auth_headers, service_payload):
        """Test GET /services/{serviceId} returns the requested service."""
        post_resp = client.post(
            f"{BASE_URL}/services",
            json=service_payload,
            headers=auth_headers(),
        )
        service_id = post_resp.json

        get_resp = client.get(
            f"{BASE_URL}/services/{service_id}",
            headers=auth_headers(),
        )
        assert get_resp.status_code == 200
        assert get_resp.json["id"] == service_id
        assert get_resp.json["name"] == service_payload["name"]

    def test_get_service_by_id_not_found(self, client, auth_headers):
        """Test GET /services/{serviceId} with unknown ID returns 404."""
        resp = client.get(
            f"{BASE_URL}/services/unknown-id-12345",
            headers=auth_headers(),
        )
        assert resp.status_code == 404
        assert resp.json["status"] == 404

    # 4. PUT /services/{serviceId} (create vs replace)
    def test_put_service_create_and_replace(
        self, client, auth_headers, service_payload
    ):
        """Test PUT /services/{serviceId} creates new service then replaces it."""
        service_id = "custom-id-999"
        payload = deepcopy(service_payload)

        # 1. PUT Create (upsert)
        create_resp = client.put(
            f"{BASE_URL}/services/{service_id}",
            json=payload,
            headers=auth_headers(),
        )
        assert create_resp.status_code == 200
        assert create_resp.json == service_id

        get_resp = client.get(
            f"{BASE_URL}/services/{service_id}",
            headers=auth_headers(),
        )
        assert get_resp.status_code == 200
        assert get_resp.json["name"] == payload["name"]

        # 2. PUT Replace (update existing)
        payload["name"] = "Updated Service Name"
        replace_resp = client.put(
            f"{BASE_URL}/services/{service_id}",
            json=payload,
            headers=auth_headers(),
        )
        assert replace_resp.status_code == 200
        assert replace_resp.json == service_id

        get_updated = client.get(
            f"{BASE_URL}/services/{service_id}",
            headers=auth_headers(),
        )
        assert get_updated.status_code == 200
        assert get_updated.json["name"] == "Updated Service Name"

    def test_put_service_validation_error(self, client, auth_headers):
        """Test PUT /services/{serviceId} with invalid body returns 400."""
        resp = client.put(
            f"{BASE_URL}/services/test-id",
            json={"invalid": True},
            headers=auth_headers(),
        )
        assert resp.status_code == 400
        assert resp.json["status"] == 400

    # 5. DELETE /services/{serviceId}
    def test_delete_service(self, client, auth_headers, service_payload):
        """Test DELETE /services/{serviceId} deletes existing service."""
        post_resp = client.post(
            f"{BASE_URL}/services",
            json=service_payload,
            headers=auth_headers(),
        )
        service_id = post_resp.json

        del_resp = client.delete(
            f"{BASE_URL}/services/{service_id}",
            headers=auth_headers(),
        )
        assert del_resp.status_code == 200
        assert del_resp.json == service_id

        # Verify service is gone
        get_resp = client.get(
            f"{BASE_URL}/services/{service_id}",
            headers=auth_headers(),
        )
        assert get_resp.status_code == 404

    def test_delete_service_not_found(self, client, auth_headers):
        """Test DELETE /services/{serviceId} with unknown ID returns 404."""
        resp = client.delete(
            f"{BASE_URL}/services/nonexistent-service",
            headers=auth_headers(),
        )
        assert resp.status_code == 404
        assert resp.json["status"] == 404

    # 6. GET /services/types
    def test_get_service_types(self, client, auth_headers, service_payload):
        """Test GET /services/types returns distinct service types."""
        client.post(
            f"{BASE_URL}/services",
            json=service_payload,
            headers=auth_headers(),
        )
        resp = client.get(f"{BASE_URL}/services/types", headers=auth_headers())
        assert resp.status_code == 200
        assert isinstance(resp.json, list)
        assert len(resp.json) >= 1
        assert resp.json[0]["group"] == service_payload["type"]["group"]
        assert resp.json[0]["artifact"] == service_payload["type"]["artifact"]

    # 7. GET /service-info & 8. POST /service-info
    def test_get_and_post_service_info(
        self, client, auth_headers, service_info_payload
    ):
        """Test GET and POST /service-info endpoints."""
        # Initial service-info loaded from config
        get_resp = client.get(f"{BASE_URL}/service-info", headers=auth_headers())
        assert get_resp.status_code == 200
        assert "id" in get_resp.json
        assert "name" in get_resp.json

        # Update service-info via POST
        updated_payload = deepcopy(service_info_payload)
        updated_payload["name"] = "Brand New Registry Name"
        post_resp = client.post(
            f"{BASE_URL}/service-info",
            json=updated_payload,
            headers=auth_headers(),
        )
        assert post_resp.status_code == 201

        # Check updated service-info
        get_updated = client.get(f"{BASE_URL}/service-info", headers=auth_headers())
        assert get_updated.status_code == 200
        assert get_updated.json["name"] == "Brand New Registry Name"


class TestAppStartup:
    """Test app.py startup and coverage."""

    def test_app_main_startup(self, monkeypatch):
        """Test app.py main() loads config, registers service-info, and runs app."""
        repo_dir = Path(__file__).resolve().parent.parent
        app_dir = repo_dir / "cloud_registry"

        monkeypatch.chdir(str(repo_dir))
        monkeypatch.syspath_prepend(str(app_dir))
        monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)

        mongo_client = mongomock.MongoClient()
        monkeypatch.setattr(flask_pymongo, "MongoClient", lambda *a, **kw: mongo_client)
        monkeypatch.setattr(auth.requests, "get", mock_oidc_get)

        run_mock = MagicMock()
        monkeypatch.setattr("connexion.apps.flask_app.FlaskApp.run", run_mock)

        app_main()
        assert run_mock.called

    def test_app_main_missing_config(self, monkeypatch):
        """Test app.py main() raises FileNotFoundError when config file is missing."""
        with patch.object(Path, "exists", return_value=False):
            with pytest.raises(FileNotFoundError):
                app_main()
