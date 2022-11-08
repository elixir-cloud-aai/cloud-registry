"""Cloud Registry custom config models."""

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
        >>> ServiceConfig(
        ...     url_prefix='https',
        ...     external_host='0.0.0.0',
        ...     external_port=8080,
        ...     api_path=''
        ... )
        ServiceConfig(url_prefix='https', external_host='0.0.0.0', external_po\
rt=8080, api_path='')
    """
    url_prefix: str
    external_host: str
    external_port: int
    api_path: str


class TypeConfig(FOCABaseConfig):
    """Model for deifining the type of GA4GH Service.

    Args:
        group: Namespace in reverse domain name format.
        artifact: Name of the API or GA4GH specification implemented.
        version: Version of the API or specification.

    Attributes:
        group: Namespace in reverse domain name format.
        artifact: Name of the API or GA4GH specification implemented.
        version: Version of the API or specification.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> TypeConfig(
        ...     group='service.group',
        ...     artifact='service_artifact',
        ...     version='1.0.0'
        ... )
        TypeConfig(group='service.group', artifact='service_artifact', version\
='1.0.0')
    """
    group: str
    artifact: str
    version: str


class OrganizationConfig(FOCABaseConfig):
    """Model for organization providing the service.

    Args:
        name: Name of the organization responsible for the service.
        url: URL of the website of the organization (RFC 3986 format).

    Attributes:
        name: Name of the organization responsible for the service.
        url: URL of the website of the organization (RFC 3986 format).

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> OrganizationConfig(
        ...     name='organization_name',
        ...     url='organization_url'
        ... )
        OrganizationConfig(name='organization_name', url='organization_url')
    """
    name: str
    url: str


class ServiceInfoConfig(FOCABaseConfig):
    """Model for service information parameters.

    Args:
        id: Unique identifier of this service.
        name: Service name.
        type: Type of GA4GH service.
        description: Service description.
        organization: Organization providing the service.
        contactUrl: URL of the contact for the provider of the service, e.g. a
            link to a contact form (RFC 3986 format), or an email (RFC 2368
            format).
        documentationUrl: URL of the documentation of the service (RFC 3986
            format). This should help someone learn how to use your service,
            including any specifics required to access data, e.g.
            authentication.
        createdAt: Timestamp describing when the service was first deployed and
            available (RFC 3339 format).
        updatedAt: Timestamp describing when the service was last updated (RFC
            3339 format).
        environment: Environment the service is running in. Use this to
            distinguish between production, development and testing/staging
            deployments. Suggested values are prod, test, dev, staging.
        version: Version of the service being described. Semantic versioning
            is recommended, but other identifiers, such as dates or commit
            hashes, are also allowed. The version should be changed whenever
            the service is updated.

    Attributes:
        id: Unique identifier of this service.
        name: Service name.
        type: Type of GA4GH service.
        description: Service description.
        organization: Organization providing the service.
        contactUrl: URL of the contact for the provider of the service, e.g. a
            link to a contact form (RFC 3986 format), or an email (RFC 2368
            format).
        documentationUrl: URL of the documentation of the service (RFC 3986
            format). This should help someone learn how to use your service,
            including any specifics required to access data, e.g.
            authentication.
        createdAt: Timestamp describing when the service was first deployed and
            available (RFC 3339 format).
        updatedAt: Timestamp describing when the service was last updated (RFC
            3339 format).
        environment: Environment the service is running in. Use this to
            distinguish between production, development and testing/staging
            deployments. Suggested values are prod, test, dev, staging.
        version: Version of the service being described. Semantic versioning
            is recommended, but other identifiers, such as dates or commit
            hashes, are also allowed. The version should be changed whenever
            the service is updated.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServiceInfoConfig(
        ...     id='service_id',
        ...     name='service_name',
        ...     type=TypeConfig(
        ...         group='service.group',
        ...         artifact='service_artifact',
        ...         version='1.0.0'
        ...     ),
        ...     description='Service description.',
        ...     organization=OrganizationConfig(
        ...         name='organization_name',
        ...         url='organization_url'
        ...     ),
        ...     contactUrl='service_contact_url',
        ...     documentationUrl='service_document_url',
        ...     createdAt='2020-11-04T12:58:19Z',
        ...     updatedAt='2020-11-04T12:58:19Z',
        ...     environment='dev',
        ...     version='1.0.0-dev-XXXXXX'
        ... )
        ServiceInfoConfig(id='service_id', name='service_name', type=TypeConfi\
g(group='service.group', artifact='service_artifact', version='1.0.0'), descri\
ption='Service description.', organization=OrganizationConfig(name='organizati\
on_name', url='organization_url'), contactUrl='service_contact_url', documenta\
tionUrl='service_document_url', createdAt='2020-11-04T12:58:19Z', updatedAt='2\
020-11-04T12:58:19Z', environment='dev', version='1.0.0-dev-XXXXXX')
    """
    id: str
    name: str
    type: TypeConfig
    description: str
    organization: OrganizationConfig
    contactUrl: str
    documentationUrl: str
    createdAt: str
    updatedAt: str
    environment: str
    version: str


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
        >>> IdConfig(
        ...     charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...     length=6
        ... )
        IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6)
    """
    charset: str
    length: int


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
        >>> MetaVersionConfig(
        ...     init=1,
        ...     increment=1
        ... )
        MetaVersionConfig(init=1, increment=1)
    """
    init: int
    increment: int


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
        >>> ServicesConfig(
        ...     id=IdConfig(
        ...         charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...         length=6
        ...     ),
        ...     meta_version=MetaVersionConfig(
        ...         init=1,
        ...         increment=1
        ...     )
        ... )
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
        >>> EndpointsConfig(
        ...     service=ServiceConfig(
        ...         url_prefix='https',
        ...         external_host='0.0.0.0',
        ...         external_port=8080,
        ...         api_path=''
        ...     ),
        ...     service_info=ServiceInfoConfig(
        ...         id='',
        ...         name='service_name',
        ...         type=TypeConfig(
        ...             group='service.group',
        ...             artifact='service_artifact',
        ...             version='1.0.0'
        ...         ),
        ...         description='Service description.',
        ...         organization=OrganizationConfig(
        ...             name='organization_name',
        ...             url='organization_url'
        ...         ),
        ...         contactUrl='service_contact_url',
        ...         documentationUrl='service_document_url',
        ...         createdAt='2020-11-04T12:58:19Z',
        ...         updatedAt='2020-11-04T12:58:19Z',
        ...         environment='dev',
        ...         version='1.0.0-dev-XXXXXX'
        ...     ),
        ...     services=ServicesConfig(
        ...         id=IdConfig(
        ...             charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...             length=6
        ...         ),
        ...         meta_version=MetaVersionConfig(
        ...             init=1,
        ...             increment=1
        ...         )
        ...     )
        ... )
        EndpointsConfig(service=ServiceConfig(url_prefix='https',external_host\
='0.0.0.0',external_port=8080,api_path=''),service_info=ServiceInfoConfig(id='\
',name='service_name',type=TypeConfig(group='service.group',artifact='service_\
artifact',version='1.0.0'),description='Service description.',organization=Org\
anizationConfig(name='organization_name',url='organization_url'),contactUrl='s\
ervice_contact_url',documentationUrl='service_document_url',createdAt='2020-11\
-04T12:58:19Z',updatedAt='2020-11-04T12:58:19Z',environment='dev',version='1.0\
.0-dev-XXXXXX'),services=ServicesConfig(id=IdConfig(charset='ABCDEFGHIJKLMNOPQ\
RSTUVWXYZ0123456789',length=6),meta_version=MetaVersionConfig(init=1,increment\
=1)))
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
        >>> CustomConfig(
        ...     endpoints=EndpointsConfig(
        ...         service=ServiceConfig(
        ...             url_prefix='https',
        ...             external_host='0.0.0.0',
        ...             external_port=8080,
        ...             api_path=''
        ...         ),
        ...         service_info=ServiceInfoConfig(
        ...             id='',
        ...             name='service_name',
        ...             type=TypeConfig(
        ...                 group='service.group',
        ...                 artifact='service_artifact',
        ...                 version='1.0.0'
        ...             ),
        ...             description='Service description.',
        ...             organization=OrganizationConfig(
        ...                 name='organization_name',
        ...                 url='organization_url'
        ...             ),
        ...             contactUrl='service_contact_url',
        ...             documentationUrl='service_document_url',
        ...             createdAt='2020-11-04T12:58:19Z',
        ...             updatedAt='2020-11-04T12:58:19Z',
        ...             environment='dev',
        ...             version='1.0.0-dev-XXXXXX'
        ...         ),
        ...         services=ServicesConfig(
        ...             id=IdConfig(
        ...                 charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...                 length=6
        ...             ),
        ...             meta_version=MetaVersionConfig(
        ...                 init=1,
        ...                 increment=1
        ...             )
        ...         )
        ...     )
        ... )
        CustomConfig(endpoints=EndpointsConfig(service=ServiceConfig(url_prefi\
x='https',external_host='0.0.0.0',external_port=8080,api_path=''),service_info\
=ServiceInfoConfig(id='',name='service_name',type=TypeConfig(group='service.gr\
oup',artifact='service_artifact',version='1.0.0'),description='Service descrip\
tion.',organization=OrganizationConfig(name='organization_name',url='organizat\
ion_url'),contactUrl='service_contact_url',documentationUrl='service_document_\
url',createdAt='2020-11-04T12:58:19Z',updatedAt='2020-11-04T12:58:19Z',environ\
ment='dev',version='1.0.0-dev-XXXXXX'),services=ServicesConfig(id=IdConfig(cha\
rset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',length=6),meta_version=MetaVersion\
Config(init=1,increment=1))))
    """
    endpoints: EndpointsConfig
