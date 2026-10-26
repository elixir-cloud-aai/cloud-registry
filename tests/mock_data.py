"""Mock data for testing."""

from typing import Any

DB = "serviceStore"
# Media type of error responses, as in `config.yaml` (`exceptions.media_type`)
ERROR_MEDIA_TYPE = "application/json"
# Size limit of request bodies, in bytes, as in `config.yaml`
# (`server.max_content_length`)
MAX_CONTENT_LENGTH = 1024 * 1024
MOCK_ID = "mock_id"
MOCK_ID_ONE_CHAR = "A"
CHARSET = "0123456789"
INDEX_CONFIG: dict[str, Any] = {"keys": [("id", 1)], "options": {"unique": True}}
COLLECTION_CONFIG: dict[str, Any] = {
    "indexes": [INDEX_CONFIG],
}
# Service types are unique as written (cf. `config.yaml`)
TYPE_INDEX_CONFIG: dict[str, Any] = {
    "keys": [("group", 1), ("artifact", 1), ("version", 1)],
    "options": {"unique": True},
}
DB_CONFIG: dict[str, Any] = {
    "collections": {
        "services": COLLECTION_CONFIG,
        "service_types": {"indexes": [TYPE_INDEX_CONFIG]},
    },
}
MONGO_CONFIG: dict[str, Any] = {
    "host": "mongodb",
    "port": 27017,
    "dbs": {
        "serviceStore": DB_CONFIG,
    },
}
# Public URL of the app (`server.public_url`), with the path prefix of a
# reverse proxy
MOCK_PUBLIC_URL = "http://1.2.3.4/registry"
SERVICES_CONFIG: dict[str, Any] = {
    "id": {
        "charset": CHARSET,
        "length": 6,
    },
}
ENDPOINT_CONFIG: dict[str, Any] = {
    "services": SERVICES_CONFIG,
}
CUSTOM_CONFIG: dict[str, Any] = {"endpoints": ENDPOINT_CONFIG}
MOCK_TYPE: dict[str, str] = {
    "group": "org.ga4gh",
    "artifact": "beacon",
    "version": "1.0.0",
}
MOCK_SERVICE: dict[str, Any] = {
    "name": "name",
    "type": MOCK_TYPE,
    "organization": "organization",
    "version": "version",
    "url": "https://beacon.example.org/api",
}
