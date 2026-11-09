"""Cloud Registry custom config models."""

import ipaddress
from typing import Annotated, Literal

from foca.models.config import FOCABaseConfig
from foca.utils.misc import generate_id
from pydantic import (
    Field,
    NonNegativeInt,
    PositiveInt,
    field_validator,
)


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


# Bounds of the time between syncs of a service, in hours: shorter intervals
# fetch from the registered services more often than service info changes, and
# longer ones overflow the times of next attempts
MIN_SYNC_INTERVAL_HOURS = 1
MAX_SYNC_INTERVAL_HOURS = 8760


class ServiceInfoConfig(FOCABaseConfig):
    """Model for fetching the service info of services.

    Args:
        private_networks: Networks (CIDR notation) that the app may fetch
            service info from, besides public addresses; none by default.
            Only for networks whose services users may probe, e.g., in tests.
        fetches_per_hour: How often each user may have service info fetched
            per hour (`429` above).
        sync: Whether services can be synced with their service info.
        sync_interval_hours: Time between syncs of a service, in hours, from
            `MIN_SYNC_INTERVAL_HOURS` (an hour) to `MAX_SYNC_INTERVAL_HOURS`
            (365 days).

    Attributes:
        private_networks: Networks (CIDR notation) that the app may fetch
            service info from, besides public addresses; none by default.
            Only for networks whose services users may probe, e.g., in tests.
        fetches_per_hour: How often each user may have service info fetched
            per hour (`429` above).
        sync: Whether services can be synced with their service info.
        sync_interval_hours: Time between syncs of a service, in hours, from
            `MIN_SYNC_INTERVAL_HOURS` (an hour) to `MAX_SYNC_INTERVAL_HOURS`
            (365 days).

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, a network is invalid (also with host bits set), or
            `fetches_per_hour` is not positive, or `sync_interval_hours` is out
            of its bounds.

    Example:
        >>> ServiceInfoConfig(private_networks=['10.1.0.0/16'])
        ServiceInfoConfig(private_networks=['10.1.0.0/16'], fetches_per_hour=30, \
sync=True, sync_interval_hours=24.0)
    """

    private_networks: list[str] = Field(default_factory=list)
    fetches_per_hour: PositiveInt = 30
    sync: bool = True
    sync_interval_hours: Annotated[
        float,
        Field(
            ge=MIN_SYNC_INTERVAL_HOURS, le=MAX_SYNC_INTERVAL_HOURS, allow_inf_nan=False
        ),
    ] = 24.0

    @field_validator("private_networks")
    @classmethod
    def _check_networks(cls, networks: list[str]) -> list[str]:
        """Reject values that aren't networks.

        Args:
            networks: Networks.

        Returns:
            The networks.

        Raises:
            ValueError: A value is not a network.
        """
        for network in networks:
            ipaddress.ip_network(network)
        return networks


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
        service_info: Fetching the service info of services.

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
        service_info: Fetching the service info of services.

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
taging'], service_info=ServiceInfoConfig(private_networks=[], fetches_per_hour\
=30, sync=True, sync_interval_hours=24.0))
    """

    id: IdConfig
    # pydantic copies mutable defaults for each instance
    url_schemes: list[Literal["http", "https"]] = Field(default=["https"], min_length=1)
    # at most as long as the API allows (`maxLength` of `environment`)
    environments: list[Annotated[str, Field(max_length=256)]] = Field(
        default=["prod", "test", "dev", "staging"]
    )
    service_info: ServiceInfoConfig = Field(default_factory=ServiceInfoConfig)


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
'prod', 'test', 'dev', 'staging'], service_info=ServiceInfoConfig(private_networks\
=[], fetches_per_hour=30, sync=True, sync_interval_hours=24.0)))
    """

    services: ServicesConfig


# Maximum days after which invitations expire (about ten years; larger values
# can overflow the expiry times of invitations)
MAX_EXPIRY_DAYS = 3650
# Maximum days for which expired invitations are kept (labels may name people,
# so there is no value for keeping them forever)
MAX_KEEP_EXPIRED_DAYS = 365


class InvitationsConfig(FOCABaseConfig):
    """Model for invitations to services (`POST /services/{id}/invitations`).

    Args:
        expiry_days: Days after which invitations expire, whether accepted
            or not (at most `MAX_EXPIRY_DAYS`); accepted ones are deleted then.
        keep_expired_days: Days for which expired invitations that weren't
            accepted are kept before they are deleted (at most
            `MAX_KEEP_EXPIRED_DAYS`; `0` to delete them when they expire).
        max_per_service: How many invitations that haven't expired, open
            or accepted, a service may have (`409` above); unlimited if
            `None`.

    Attributes:
        expiry_days: Days after which invitations expire, whether accepted
            or not (at most `MAX_EXPIRY_DAYS`); accepted ones are deleted then.
        keep_expired_days: Days for which expired invitations that weren't
            accepted are kept before they are deleted (at most
            `MAX_KEEP_EXPIRED_DAYS`; `0` to delete them when they expire).
        max_per_service: How many invitations that haven't expired, open
            or accepted, a service may have (`409` above); unlimited if
            `None`.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, `expiry_days` is not positive or larger than
            `MAX_EXPIRY_DAYS`, `keep_expired_days` is negative or larger than
            `MAX_KEEP_EXPIRED_DAYS`, or `max_per_service` is negative.

    Example:
        >>> InvitationsConfig(expiry_days=1)
        InvitationsConfig(expiry_days=1, keep_expired_days=30, max_per_service=20)
    """

    expiry_days: Annotated[int, Field(gt=0, le=MAX_EXPIRY_DAYS)] = 7
    keep_expired_days: Annotated[int, Field(ge=0, le=MAX_KEEP_EXPIRED_DAYS)] = 30
    max_per_service: NonNegativeInt | None = 20


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
            has a user other than an admin may have added, in modes `open`
            and `moderated` (`409` above; served as `0` in mode
            `controlled`); unlimited if `None`. Bounds the lists of types,
            and of pending types, that users can grow.
        max_services_per_user: How many of the registered services a user
            other than an admin may have registered (`409` above);
            unlimited if `None`, admins only if `0`.
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
            has a user other than an admin may have added, in modes `open`
            and `moderated` (`409` above; served as `0` in mode
            `controlled`); unlimited if `None`. Bounds the lists of types,
            and of pending types, that users can grow.
        max_services_per_user: How many of the registered services a user
            other than an admin may have registered (`409` above);
            unlimited if `None`, admins only if `0`.
        invitations: Invitations to services.

    Raises:
        pydantic.ValidationError: The class was instantianted with an illegal
            data type, `service_types` is not `open`, `controlled` or
            `moderated`, or `max_unused_service_types` or
            `max_services_per_user` is negative.

    Example:
        >>> AccessControlConfig(service_types='controlled')
        AccessControlConfig(service_types='controlled', max_unused_service_types=10, \
max_services_per_user=50, invitations=InvitationsConfig(expiry_days=7, \
keep_expired_days=30, max_per_service=20))
    """

    service_types: Literal["open", "controlled", "moderated"] = "open"
    max_unused_service_types: NonNegativeInt | None = 10
    max_services_per_user: NonNegativeInt | None = 50
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
https'], environments=['prod', 'test', 'dev', 'staging'], service_info=ServiceI\
nfoConfig(private_networks=[], fetches_per_hour=30, sync=True, sync_interval_h\
ours=24.0))), access_control=AccessControlConfig(service_types='open', max_unu\
sed_service_types=10, max_services_per_user=50, invitations=InvitationsConfig(\
expiry_days=7, keep_expired_d\
ays=30, max_per_service=20)))
    """

    endpoints: EndpointsConfig
    access_control: AccessControlConfig = Field(default_factory=AccessControlConfig)
