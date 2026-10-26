"""Service info fields of the registry, computed from the config.

FOCA serves the service info (`GET /service-info`) from the config
(`service_info.service`), with the fields that `runtime_fields()` returns
(`service_info.runtime_fields` in `config.yaml`; called on each request):
the rules for registrations (`registration_rules`), an extension of the GA4GH
`Service` schema (`RegistryServiceInfo` in `api/additions.openapi.yaml`).
They are built from the config in effect, so that clients (e.g., UIs) can
read them instead of configuring them: the environments
(`custom.endpoints.services.environments`) and service URL schemes
(`url_schemes`) allowed, who may add service types
(`custom.access_control.service_types`), and how many unused types users
other than admins may have added (`max_unused_service_types`).
"""

from typing import Any

from foca import get_config

from cloud_registry.utils import get_custom_config

# Service info field with the registration rules
RULES = "registration_rules"
# Path of the function returning the fields (`service_info.runtime_fields`)
RUNTIME_FIELDS = f"{__name__}.runtime_fields"


def registration_rules() -> dict[str, Any]:
    """Get the rules for registrations of the config in effect.

    Must be called in an app context.

    Returns:
        Environments allowed for services (`environments`; empty if any
        value is), schemes allowed in service URLs (`url_schemes`), who may
        add service types (`service_types`: `open`, `controlled` or
        `moderated`), and how many types that no service has users other
        than admins may have added (`max_unused_service_types`; left out if
        there is no limit; `0` if they can't add types). Lists are copies.
    """
    custom = get_custom_config()
    services = custom.endpoints.services
    access_control = custom.access_control
    rules: dict[str, Any] = {
        "environments": list(services.environments),
        "url_schemes": list(services.url_schemes),
        "service_types": access_control.service_types,
    }
    # FOCA leaves out `None` values of the service info anyway
    if access_control.max_unused_service_types is not None:
        rules["max_unused_service_types"] = access_control.max_unused_service_types
    return rules


def runtime_fields() -> dict[str, Any]:
    """Get the service info fields computed at runtime.

    Called by FOCA in an app context, on startup and for each request for
    the service info (`service_info.runtime_fields`).

    Returns:
        The registration rules (cf. `registration_rules()`).
    """
    return {RULES: registration_rules()}


def check_service_info_fields() -> None:
    """Check that the service info has the registration rules of the app.

    Must be called in an app context.

    Raises:
        RuntimeError: No service info is configured, its fields aren't
            computed by `runtime_fields()` (`service_info.runtime_fields`),
            or the config sets the registration rules
            (`service_info.service.registration_rules`, also to `null`),
            which would take precedence over those of the app, so that they
            could differ from the rules the registry enforces.
    """
    service_info = get_config().service_info
    if (
        service_info is None
        or service_info.runtime_fields != RUNTIME_FIELDS
        or RULES in service_info.service
    ):
        raise RuntimeError(
            f"The service info must serve the app's `{RULES}`: keep "
            f"`service_info.runtime_fields` ('{RUNTIME_FIELDS}') and don't set "
            f"`service_info.service.{RULES}`"
        )
