import string

from foca.models.config import FOCABaseConfig


class ServiceConfig(FOCABaseConfig):
    url_prefix: str = 'https'
    external_host: str = '0.0.0.0'
    external_port: int = 8080
    api_path: str = ''


class TypeConfig(FOCABaseConfig):
    group: str = "org.ga4gh"
    artifact: str = "service-registry"
    version: str = "1.0.0"

    
class OrganizationConfig(FOCABaseConfig):
    name: str = "ELIXIR Cloud & AAI"
    url: str = "https://github.com/elixir-cloud-aai/elixir-cloud-aai"


class ServiceInfoConfig(FOCABaseConfig):
    id: str = "ELIXIR_CLOUD_SERVICE_REGISTRY_1"
    name: str = "ELIXIR_CLOUD"
    type: TypeConfig = TypeConfig()
    description: str = "Service registry for the ELIXIR Cloud network."
    organization: OrganizationConfig = OrganizationConfig()
    contactUrl: str = "https://github.com/elixir-cloud-aai/elixir-cloud-aai"
    documentationUrl: str = "https://github.com/elixir-cloud-aai/elixir-cloud-aai"
    createdAt: str = '2020-11-04T12:58:19Z'
    updatedAt: str = '2020-11-04T12:58:19Z'
    environment: str = "dev"
    version: str = "1.0.0-dev-201109"


class IdConfig(FOCABaseConfig):
    charset: str = string.ascii_uppercase + string.digits
    length: int = 6


class MetaVersionConfig(FOCABaseConfig):
    init: int = 1
    increment: int = 1

class ServicesConfig(FOCABaseConfig):
    id: IdConfig = IdConfig()
    meta_version: MetaVersionConfig = MetaVersionConfig()


class EndpointsConfig(FOCABaseConfig):
    service: ServiceConfig = ServiceConfig()
    service_info: ServiceInfoConfig = ServiceInfoConfig()
    services: ServicesConfig = ServicesConfig()


class CustomConfig(FOCABaseConfig):
    endpoints: EndpointsConfig