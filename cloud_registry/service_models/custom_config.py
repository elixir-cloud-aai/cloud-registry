"""Cloud Registry custom config models."""

from foca.models.config import FOCABaseConfig
from foca.utils.misc import generate_id
from pydantic import Field, PositiveInt, field_validator

# Default size limit of request bodies, in bytes (1 MiB)
MAX_CONTENT_LENGTH = 1024 * 1024


class ServiceConfig(FOCABaseConfig):
    """Model for configuration parameters to set up a service.

    Args:
        url_prefix: Service protocol at which the application is exposed.
        external_host: Host at which the application is exposed.
        external_port: Port at which the application is exposed.
        api_path: Path prefix in front of the API base path in public URLs,
            e.g., when served behind a reverse proxy; empty for none.

    Attributes:
        url_prefix: Service protocol at which the application is exposed.
        external_host: Host at which the application is exposed.
        external_port: Port at which the application is exposed.
        api_path: Path prefix in front of the API base path in public URLs,
            e.g., when served behind a reverse proxy; empty for none.

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


class IdConfig(FOCABaseConfig):
    """Model for defining unique identifier for services on cloud registry.

    Args:
        charset: A string of allowed characters.
        length: Length of returned string.

    Attributes:
        charset: A string of allowed characters.
        length: Length of returned string.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or `charset` is empty, an expression (such as
            `string.digits`), which FOCA no longer evaluates, contains `/` or
            consists of dots only, as identifiers are URL path segments.

    Example:
        >>> IdConfig(
        ...     charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...     length=6
        ... )
        IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6)
    """

    charset: str
    length: PositiveInt

    @field_validator("charset")
    @classmethod
    def _check_charset(cls, charset: str) -> str:
        """Reject character sets that identifiers cannot be generated from,
        or that give identifiers that aren't URL path segments.
        """
        if "/" in charset:
            raise ValueError("Identifier character sets must not contain '/'")
        if charset and not charset.strip("."):
            raise ValueError("Identifier character sets must not be dots only")
        try:
            generate_id(charset=charset, length=1)
        except TypeError as exc:  # pydantic only reports `ValueError`s
            raise ValueError(str(exc)) from exc
        return charset


class ServicesConfig(FOCABaseConfig):
    """Model for defining the service database store for cloud registry. This
    defines the configurations for service identifiers stored on cloud
    registry.

    Args:
        id: Unique identifier for a service in cloud registry.

    Attributes:
        id: Unique identifier for a service in cloud registry.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> ServicesConfig(
        ...     id=IdConfig(
        ...         charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...         length=6
        ...     )
        ... )
        ServicesConfig(id=IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567\
89', length=6))
    """

    id: IdConfig


class EndpointsConfig(FOCABaseConfig):
    """Model for defining endpoint configuration parameters.

    Args:
        service: Service run parameters.
        services: Cloud registry service store parameters.

    Attributes:
        service: Service run parameters.
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
        ...     services=ServicesConfig(
        ...         id=IdConfig(
        ...             charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...             length=6
        ...         )
        ...     )
        ... )
        EndpointsConfig(service=ServiceConfig(url_prefix='https', external_hos\
t='0.0.0.0', external_port=8080, api_path=''), services=ServicesConfig(id=IdCo\
nfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6)))
    """

    service: ServiceConfig
    services: ServicesConfig


class AccessControlConfig(FOCABaseConfig):
    """Model for access control settings of the app.

    Args:
        require_admin: Whether the app refuses to start unless a group is
            mapped to the role `admin` (in
            `security.access_control.group_mapping`).

    Attributes:
        require_admin: Whether the app refuses to start unless a group is
            mapped to the role `admin` (in
            `security.access_control.group_mapping`).

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> AccessControlConfig(require_admin=False)
        AccessControlConfig(require_admin=False)
    """

    require_admin: bool = True


class CustomConfig(FOCABaseConfig):
    """Model for defining the custom configurations for cloud registry.

    Args:
        endpoints: Endpoint service configurations for cloud registry.
        access_control: Access control settings of the app.
        max_content_length: Size limit of request bodies, in bytes; larger
            requests are rejected with `413`, before authentication.

    Attributes:
        endpoints: Endpoint service configurations for cloud registry.
        access_control: Access control settings of the app.
        max_content_length: Size limit of request bodies, in bytes; larger
            requests are rejected with `413`, before authentication.

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
        ...         services=ServicesConfig(
        ...             id=IdConfig(
        ...                 charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...                 length=6
        ...             )
        ...         )
        ...     )
        ... )
        CustomConfig(endpoints=EndpointsConfig(service=ServiceConfig(url_prefi\
x='https', external_host='0.0.0.0', external_port=8080, api_path=''), services\
=ServicesConfig(id=IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', le\
ngth=6))), access_control=AccessControlConfig(require_admin=True), max_content\
_length=1048576)
    """

    endpoints: EndpointsConfig
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)
    max_content_length: PositiveInt = MAX_CONTENT_LENGTH
