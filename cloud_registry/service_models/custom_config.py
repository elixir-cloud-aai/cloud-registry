"""Cloud Registry custom config models."""

import string

from foca.models.config import FOCABaseConfig


class ServiceConfig(FOCABaseConfig):
    """Model for configuration parameters to set up a service.

    Args:
        url_prefix: Service protocol at which the application is exposed.
        external_host: Host at which the application is exposed.
        external_port: Port at which the application is exposed.
        api_path: API Path at which service resources are exposed.

    Attributes:
        url_prefix: Service protocol at which the application is exposed.
        external_host: Host at which the application is exposed.
        external_port: Port at which the application is exposed.
        api_path: API Path at which service resources are exposed.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServiceConfig()
        ServiceConfig(url_prefix='https', external_host='0.0.0.0', external_po\
rt=8080, api_path='')
    """
    url_prefix: str = 'https'
    external_host: str = '0.0.0.0'
    external_port: int = 8080
    api_path: str = ''


class TypeConfig(FOCABaseConfig):
    """Model for service type definitions.

    Args:
        group: Service group.
        artifact: Service group artifact.
        version: Service group version.

    Attributes:
        group: Service group.
        artifact: Service group artifact.
        version: Service group version.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> TypeConfig()
        TypeConfig(group='org.ga4gh', artifact='service-registry', version='1.\
0.0')
    """
    group: str = "org.ga4gh"
    artifact: str = "service-registry"
    version: str = "1.0.0"


class OrganizationConfig(FOCABaseConfig):
    """Model for storing service organization information.

    Args:
        name: Organization name.
        url: Organization url.

    Attributes:
        name: Organization name.
        url: Organization url.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> OrganizationConfig()
        OrganizationConfig(name='ELIXIR Cloud & AAI', url='https://github.com/\
elixir-cloud-aai/elixir-cloud-aai')
    """
    name: str = "ELIXIR Cloud & AAI"
    url: str = "https://github.com/elixir-cloud-aai/elixir-cloud-aai"


class ServiceInfoConfig(FOCABaseConfig):
    """Model for service specific parameters.

    Args:
        id: Service identifier.
        name: Service name.
        type: Service type parameters.
        description: Service description.
        organization: Service organization parameters.
        contactUrl: Service contact URL.
        documentationUrl: Service documentation URL.
        createdAt: Service creation timestamp.
        updatedAt: Service updation timestamp.
        environment: Service environment.
        version: Service version.

    Attributes:
        id: Service identifier.
        name: Service name.
        type: Service type parameters.
        description: Service description.
        organization: Service organization parameters.
        contactUrl: Service contact URL.
        documentationUrl: Service documentation URL.
        createdAt: Service creation timestamp.
        updatedAt: Service updation timestamp.
        environment: Service environment.
        version: Service version.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServiceInfoConfig()
        ServiceInfoConfig(id='ELIXIR_CLOUD_SERVICE_REGISTRY_1', name='ELIXIR_C\
LOUD', type=TypeConfig(group='org.ga4gh', artifact='service-registry', version\
='1.0.0'), description='Service registry for the ELIXIR Cloud network.', organ\
ization=OrganizationConfig(name='ELIXIR Cloud & AAI', url='https://github.com/\
elixir-cloud-aai/elixir-cloud-aai'), contactUrl='https://github.com/elixir-clo\
ud-aai/elixir-cloud-aai', documentationUrl='https://github.com/elixir-cloud-aa\
i/elixir-cloud-aai', createdAt='2020-11-04T12:58:19Z', updatedAt='2020-11-04T1\
2:58:19Z', environment='dev', version='1.0.0-dev-201109')
    """
    id: str
    name: str = "ELIXIR_CLOUD"
    type: TypeConfig = TypeConfig()
    description: str = "Service registry for the ELIXIR Cloud network."
    organization: OrganizationConfig = OrganizationConfig()
    contactUrl: str = "https://github.com/elixir-cloud-aai"
    documentationUrl: str = "https://github.com/elixir-cloud-aai"
    createdAt: str = '2020-11-04T12:58:19Z'
    updatedAt: str = '2020-11-04T12:58:19Z'
    environment: str = "dev"
    version: str = "1.0.0-dev-201109"


class IdConfig(FOCABaseConfig):
    """Model for defining unique identifier for services on cloud registry.

    Args:
        charset: A string of allowed characters or an expression evaluating to
            a string of allowed characters.
        length: Length of returned string.

    Attributes:
        charset: A string of allowed characters or an expression evaluating to
            a string of allowed characters.
        length: Length of returned string.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> IdConfig()
        IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6)
    """
    charset: str = string.ascii_uppercase + string.digits
    length: int = 6


class MetaVersionConfig(FOCABaseConfig):
    """Model for storing version control configurations for services onboarded
    on cloud registry.

    Args:
        init: Initial version number for a given service.
        increment: Incremental value for next version.

    Attributes:
        init: Initial version number for a given service.
        increment: Incremental value for next versions.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> MetaVersionConfig()
        MetaVersionConfig(init=1, increment=1)
    """
    init: int = 1
    increment: int = 1


class ServicesConfig(FOCABaseConfig):
    """Model for defining the service database store for cloud registry. This
    defines the configurations for service identifiers stored on cloud
    registry.

    Args:
        id: Unique identifier for a service in cloud registry.
        meta_version: Version increment configuration for service upgrades.

    Attributes:
        id: Unique identifier for a service in cloud registry.
        meta_version: Version increment configuration for service upgrades.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServicesConfig()
        ServicesConfig(id=IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567\
89', length=6), meta_version=MetaVersionConfig(init=1, increment=1))
    """
    id: IdConfig
    meta_version: MetaVersionConfig


class EndpointsConfig(FOCABaseConfig):
    """Model for defining endpoint configuration parameters.

    Args:
        service: Service run parameters.
        service_info: Service info parameters.
        services: Cloud registry service store parameters.

    Attributes:
        service: Service run parameters.
        service_info: Service info parameters.
        services: Cloud registry service store parameters.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> EndpointsConfig()
        EndpointsConfig(service=ServiceConfig(url_prefix='https', external_hos\
t='0.0.0.0', external_port=8080, api_path=''), service_info=ServiceInfoConfig(\
id='ELIXIR_CLOUD_SERVICE_REGISTRY_1', name='ELIXIR_CLOUD', type=TypeConfig(gro\
up='org.ga4gh', artifact='service-registry', version='1.0.0'), description='Se\
rvice registry for the ELIXIR Cloud network.', organization=OrganizationConfig\
(name='ELIXIR Cloud & AAI', url='https://github.com/elixir-cloud-aai/elixir-cl\
oud-aai'), contactUrl='https://github.com/elixir-cloud-aai/elixir-cloud-aai', \
documentationUrl='https://github.com/elixir-cloud-aai/elixir-cloud-aai', creat\
edAt='2020-11-04T12:58:19Z', updatedAt='2020-11-04T12:58:19Z', environment='de\
v', version='1.0.0-dev-201109'), services=ServicesConfig(id=IdConfig(charset='\
ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6), meta_version=MetaVersionConf\
ig(init=1, increment=1)))
    """
    service: ServiceConfig
    service_info: ServiceInfoConfig
    services: ServicesConfig


class CustomConfig(FOCABaseConfig):
    """Model for defining the custom configurations for cloud registry.

    Args:
        endpoints: Endpoint service configurations for cloud registry.

    Attributes:
        endpoints: Endpoint service configurations for cloud registry.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> CustomConfig()
        CustomConfig(endpoints=EndpointsConfig(service=ServiceConfig(url_prefi\
x='https', external_host='0.0.0.0', external_port=8080, api_path=''), service_\
info=ServiceInfoConfig(id='ELIXIR_CLOUD_SERVICE_REGISTRY_1', name='ELIXIR_CLOU\
D', type=TypeConfig(group='org.ga4gh', artifact='service-registry', version='1\
.0.0'), description='Service registry for the ELIXIR Cloud network.', organiza\
tion=OrganizationConfig(name='ELIXIR Cloud & AAI', url='https://github.com/eli\
xir-cloud-aai/elixir-cloud-aai'), contactUrl='https://github.com/elixir-cloud-\
aai/elixir-cloud-aai', documentationUrl='https://github.com/elixir-cloud-aai/e\
lixir-cloud-aai', createdAt='2020-11-04T12:58:19Z', updatedAt='2020-11-04T12:5\
8:19Z', environment='dev', version='1.0.0-dev-201109'), services=ServicesConfi\
g(id=IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6), meta_\
version=MetaVersionConfig(init=1, increment=1))))
    """
    endpoints: EndpointsConfig
