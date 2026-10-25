"""App factory, and entry point for development.

The image serves the app with Gunicorn (`cloud_registry.app:create_app()`, see
`cloud_registry.gunicorn_config`); `python -m cloud_registry.app` runs it with
Flask's development server.
"""

import logging
from pathlib import Path

from connexion import App
from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
from foca import Foca, get_config

from cloud_registry.exceptions import validation_problem
from cloud_registry.ga4gh.registry.permissions import (
    check_admin_groups,
    check_no_service_clients,
)
from cloud_registry.ga4gh.registry.service import (
    add_service_keys,
    warn_invalid_urls,
    warn_url_schemes,
)
from cloud_registry.ga4gh.registry.service_types import add_service_types

logger = logging.getLogger(__name__)
# Logger of Connexion's request validation
VALIDATION_LOGGER = "connexion.decorators.validation"
# Placeholders of the service info in `config.yaml`, with reserved example
# domains (RFC 2606), which deployments replace
PLACEHOLDER_ID = "org.example.cloud-registry"
PLACEHOLDER_ORGANIZATION = {
    "name": "Example Organization",
    "url": "https://example.org",
}


def warn_placeholder_service_info() -> None:
    """Warn if the service info still has the placeholders of `config.yaml`.

    That is, if `id` is the placeholder, or the name or URL of `organization`
    is; the warning names the fields to set. Must be called in an app
    context.
    """
    service_info = get_config().service_info
    service = service_info.service if service_info is not None else {}
    fields = []
    if service.get("id") == PLACEHOLDER_ID:
        fields.append("`id`")
    organization = service.get("organization")
    if isinstance(organization, dict) and any(
        organization.get(key) == value
        for key, value in PLACEHOLDER_ORGANIZATION.items()
    ):
        fields.append("`organization`")
    if fields:
        logger.warning(
            "The service info (`service_info.service`) has placeholder values; "
            "set %s to describe the deployment (and `name`, `description`, "
            "`contactUrl` and `environment`)",
            ", ".join(fields),
        )


def create_app() -> App:
    """Create the app from the config, with the registry's checks.

    Adds the error handler for validation errors (silencing Connexion's
    validation log messages), checks the admin groups and the group claim,
    and that no service clients are configured, requires a size limit of
    request bodies, adds missing keys of services and the types of
    registered services that aren't stored (e.g., on upgrades), and warns of
    stored services with invalid URLs or URL schemes that are not allowed and
    of placeholders in the service info. FOCA validates the config, e.g., that
    token issuers are trusted (needed for access control) with an audience,
    and the service info (`service_info`), and limits the size of request
    bodies (`server.max_content_length`).

    Gunicorn calls this in each worker, after forking (MongoDB clients must
    not be shared across forks), so the checks run in each worker; if they
    fail, the worker fails to boot and Gunicorn exits.

    Returns:
        Connexion app.

    Raises:
        RuntimeError: Request bodies are not limited in size
            (`server.max_content_length` is null), groups are mapped
            without a group claim, no group is mapped to the role `admin`
            (unless admins are optional), service clients are configured
            (which the app doesn't support), or stored services are invalid
            or duplicates.
        pydantic.ValidationError: The config is invalid, e.g., no token
            issuers are trusted, or the service info is invalid (naming the
            invalid field).
        ValueError: The custom config (`custom`) is invalid, e.g., it still
            sets the service info (`custom.endpoints.service_info`) or the
            size limit of request bodies (`custom.max_content_length`).
    """
    foca = Foca(
        config_file=Path(__file__).parent / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()
    # Requests rejected by Connexion's validation (e.g., of the schema) get
    # the reason as detail; FOCA's handler (for all other exceptions) serves
    # the fixed details of the exception class
    for exception in (BadRequestProblem, ExtraParameterProblem):
        app.add_error_handler(exception, validation_problem)
    # Connexion's validation logs the validator's message (`ERROR`) before
    # the handler, unescaped and in full (validators quote rejected values,
    # e.g., a whole request body); the handler logs it shortened and
    # escaped. Set here, as FOCA's log config has no `loggers`
    logging.getLogger(VALIDATION_LOGGER).setLevel(logging.CRITICAL)

    with app.app.app_context():
        check_admin_groups()
        check_no_service_clients()
        # `null` would remove FOCA's size limit of request bodies, so that
        # bodies of any size are read before the access token is checked
        if get_config().server.max_content_length is None:
            raise RuntimeError(
                "Request bodies must be limited in size: set "
                "`server.max_content_length` (bytes), not null"
            )
        add_service_keys()
        add_service_types()
        warn_invalid_urls()
        warn_url_schemes()
        warn_placeholder_service_info()
    return app


def main() -> None:
    """Run the app with Flask's development server, on the configured port.

    For development; not for production.
    """
    app = create_app()
    app.run(port=app.port)


if __name__ == "__main__":
    main()
