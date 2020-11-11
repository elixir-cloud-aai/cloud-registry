"""Mock data for testing."""

DB = "serviceStore"
MOCK_ID = "mock_id"
MOCK_ID_ONE_CHAR = "A"
CHARSET_EXPRESSION = 'string.digits'
INDEX_CONFIG = {
    'keys': [('id', 1)]
}
COLLECTION_CONFIG = {
    'indexes': [INDEX_CONFIG],
}
DB_CONFIG = {
    'collections': {
        'service_info': COLLECTION_CONFIG,
        'services': COLLECTION_CONFIG,
    },
}
MONGO_CONFIG = {
    'host': 'mongodb',
    'port': 27017,
    'dbs': {
        'serviceStore': DB_CONFIG,
    },
}
SERVICE_CONFIG = {
    "url_prefix": "http",
    "external_host": "1.2.3.4",
    "external_port": 80,
    "api_path": "ga4gh/registry/v1",
}
SERVICE_INFO_CONFIG = {
    "contactUrl": "mailto:support@example.com",
    "createdAt": "2019-06-04T12:58:19Z",
    "description": "This service provides...",
    "documentationUrl": "https://docs.myservice.example.com",
    "environment": "test",
    "id": "org.ga4gh.myservice",
    "name": "My project",
    "organization": {
        "name": "My organization",
        "url": "https://example.com",
    },
    "type": {
        "artifact": "beacon",
        "group": "org.ga4gh",
        "version": "1.0.0",
    },
    "updatedAt": "2019-06-04T12:58:19Z",
    "version": "1.0.0",
}
SERVICES_CONFIG = {
    "id": {
        "charset": CHARSET_EXPRESSION,
        "length": 6,
    },
    "meta_version": {
        "init": 1,
        "increment": 1,
    },
}
ENDPOINT_CONFIG = {
    "service": SERVICE_CONFIG,
    "service_info": SERVICE_INFO_CONFIG,
    "services": SERVICES_CONFIG,
}
HEADERS_SERVICE_INFO = {
    'Content-type': 'application/json',
    'Location': (
        f"{SERVICE_CONFIG['url_prefix']}://{SERVICE_CONFIG['external_host']}:"
        f"{SERVICE_CONFIG['external_port']}/{SERVICE_CONFIG['api_path']}/"
        "service-info"
    )
}
MOCK_TYPE = {
    'group': 'org.ga4gh',
    'artifact': 'beacon',
    'version': '1.0.0'
}
MOCK_SERVICE = {
    "name": "name",
    "type": MOCK_TYPE,
    "organization": "organization",
    "version": "version",
}
