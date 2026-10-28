"""Cloud Registry custom config models."""

from typing import Annotated, Literal

from foca.models.config import FOCABaseConfig
from foca.utils.misc import generate_id
from pydantic import Field, NonNegativeInt, PositiveInt, field_validator


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
        """Reject character sets unfit for identifiers.

        Identifiers must be generated from them, and must be URL path segments.

        Args:
            charset: Character set of identifiers.

        Returns:
            The character set.

        Raises:
            ValueError: Identifiers cannot be generated from the character set,
                or would not be URL path segments.
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
    """Model for defining the service database store for cloud registry.

    Defines the configurations for service identifiers stored on cloud
    registry, the schemes allowed in service URLs and the environments
    allowed for services.

    Args:
        id: Unique identifier for a service in cloud registry.
        url_schemes: Schemes allowed in service URLs (`url`), at least one of
            `http` and `https`; `https` only by default, as clients send
            access tokens to the registered services.
        environments: Values allowed for the environment of services
            (`environment`), e.g., to keep filters consistent; by default,
            the values that the GA4GH specification suggests (`prod`, `test`,
            `dev`, `staging`). Empty to allow any value. At most 256
            characters each, as in the API.

    Attributes:
        id: Unique identifier for a service in cloud registry.
        url_schemes: Schemes allowed in service URLs (`url`), at least one of
            `http` and `https`; `https` only by default, as clients send
            access tokens to the registered services.
        environments: Values allowed for the environment of services
            (`environment`), e.g., to keep filters consistent; by default,
            the values that the GA4GH specification suggests (`prod`, `test`,
            `dev`, `staging`). Empty to allow any value. At most 256
            characters each, as in the API.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, or `url_schemes` is empty or has other schemes than
            `http` and `https` (lowercase).

    Example:
        >>> ServicesConfig(
        ...     id=IdConfig(
        ...         charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...         length=6
        ...     )
        ... )
        ServicesConfig(id=IdConfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ01234567\
89', length=6), url_schemes=['https'], environments=['prod', 'test', 'dev', 's\
taging'])
    """

    id: IdConfig
    # pydantic copies mutable defaults for each instance
    url_schemes: list[Literal["http", "https"]] = Field(default=["https"], min_length=1)
    # at most as long as the API allows (`maxLength` of `environment`)
    environments: list[Annotated[str, Field(max_length=256)]] = Field(
        default=["prod", "test", "dev", "staging"]
    )


class EndpointsConfig(FOCABaseConfig):
    """Model for defining endpoint configuration parameters.

    Args:
        services: Cloud registry service store parameters.

    Attributes:
        services: Cloud registry service store parameters.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> EndpointsConfig(
        ...     services=ServicesConfig(
        ...         id=IdConfig(
        ...             charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...             length=6
        ...         )
        ...     )
        ... )
        EndpointsConfig(services=ServicesConfig(id=IdConfig(charset='ABCDEFGHI\
JKLMNOPQRSTUVWXYZ0123456789', length=6), url_schemes=['https'], environments=[\
'prod', 'test', 'dev', 'staging']))
    """

    services: ServicesConfig


class InvitationsConfig(FOCABaseConfig):
    """Model for invitations to services (`POST /services/{id}/invitations`).

    Args:
        expiry_days: Days after which invitations expire, whether accepted
            or not; expired ones are deleted.
        max_open_per_service: How many open invitations (neither accepted
            nor expired) a service may have (`409` above); unlimited if
            `None`.

    Attributes:
        expiry_days: Days after which invitations expire, whether accepted
            or not; expired ones are deleted.
        max_open_per_service: How many open invitations (neither accepted
            nor expired) a service may have (`409` above); unlimited if
            `None`.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, `expiry_days` is not positive, or
            `max_open_per_service` is negative.

    Example:
        >>> InvitationsConfig(expiry_days=1)
        InvitationsConfig(expiry_days=1, max_open_per_service=20)
    """

    expiry_days: PositiveInt = 7
    max_open_per_service: NonNegativeInt | None = 20


class AccessControlConfig(FOCABaseConfig):
    """Model for access control settings of the app.

    Args:
        service_types: Who may add service types: `open` (the default), any
            authenticated user, explicitly (`POST /services/types`) or by
            registering a service with a new type; `controlled`, admins
            only, and services of other users must have a registered type;
            `moderated`, any authenticated user, but types of users other
            than admins are pending until an admin approves them, and so are
            the services that have them.
        max_unused_service_types: How many service types that no service
            has a user other than an admin may have added, in every mode
            (`409` above); unlimited if `None`. Bounds the lists of types,
            and of pending types, that users can grow.
        invitations: Invitations to services.

    Attributes:
        service_types: Who may add service types: `open` (the default), any
            authenticated user, explicitly (`POST /services/types`) or by
            registering a service with a new type; `controlled`, admins
            only, and services of other users must have a registered type;
            `moderated`, any authenticated user, but types of users other
            than admins are pending until an admin approves them, and so are
            the services that have them.
        max_unused_service_types: How many service types that no service
            has a user other than an admin may have added, in every mode
            (`409` above); unlimited if `None`. Bounds the lists of types,
            and of pending types, that users can grow.
        invitations: Invitations to services.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, `service_types` is not `open`, `controlled` or
            `moderated`, or `max_unused_service_types` is negative.

    Example:
        >>> AccessControlConfig(service_types='controlled')
        AccessControlConfig(service_types='controlled', max_unused_service_types=10, \
invitations=InvitationsConfig(expiry_days=7, max_open_per_service=20))
    """

    service_types: Literal["open", "controlled", "moderated"] = "open"
    max_unused_service_types: NonNegativeInt | None = 10
    invitations: InvitationsConfig = Field(default_factory=InvitationsConfig)


class CustomConfig(FOCABaseConfig):
    """Model for defining the custom configurations for cloud registry.

    Args:
        endpoints: Endpoint service configurations for cloud registry.
        access_control: Access control settings of the app.

    Attributes:
        endpoints: Endpoint service configurations for cloud registry.
        access_control: Access control settings of the app.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type.

    Example:
        >>> CustomConfig(
        ...     endpoints=EndpointsConfig(
        ...         services=ServicesConfig(
        ...             id=IdConfig(
        ...                 charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
        ...                 length=6
        ...             )
        ...         )
        ...     )
        ... )
        CustomConfig(endpoints=EndpointsConfig(services=ServicesConfig(id=IdCo\
nfig(charset='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', length=6), url_schemes=['\
https'], environments=['prod', 'test', 'dev', 'staging'])), access_control=Acc\
essControlConfig(service_types='open', max_unused_service_types=10, invitations=I\
nvitationsConfig(expiry_days=7, max_open_per_service=20)))
    """

    endpoints: EndpointsConfig
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)
