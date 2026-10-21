"""Mock data for testing."""

DB = "serviceStore"
# Media type of error responses, as in `config.yaml` (`exceptions.media_type`)
ERROR_MEDIA_TYPE = "application/json"
# Size limit of request bodies, in bytes, as in `config.yaml`
# (`server.max_content_length`)
MAX_CONTENT_LENGTH = 1024 * 1024
MOCK_ID = "mock_id"
MOCK_ID_ONE_CHAR = "A"
CHARSET = "0123456789"
INDEX_CONFIG = {"keys": [("id", 1)]}
COLLECTION_CONFIG = {
    "indexes": [INDEX_CONFIG],
}
DB_CONFIG = {
    "collections": {
        "services": COLLECTION_CONFIG,
    },
}
MONGO_CONFIG = {
    "host": "mongodb",
    "port": 27017,
    "dbs": {
        "serviceStore": DB_CONFIG,
    },
}
SERVICE_CONFIG = {
    "url_prefix": "http",
    "external_host": "1.2.3.4",
    "external_port": 80,
    # Path prefix of a reverse proxy
    "api_path": "registry",
}
# Public URL of the app with `SERVICE_CONFIG`
MOCK_PUBLIC_URL = "http://1.2.3.4:80/registry"
SERVICES_CONFIG = {
    "id": {
        "charset": CHARSET,
        "length": 6,
    },
}
ENDPOINT_CONFIG = {
    "service": SERVICE_CONFIG,
    "services": SERVICES_CONFIG,
}
CUSTOM_CONFIG = {"endpoints": ENDPOINT_CONFIG}
MOCK_TYPE = {"group": "org.ga4gh", "artifact": "beacon", "version": "1.0.0"}
MOCK_SERVICE = {
    "name": "name",
    "type": MOCK_TYPE,
    "organization": "organization",
    "version": "version",
    "url": "https://beacon.example.org/api",
}
