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
from cloud_registry.ga4gh.registry.permissions import check_admin_groups
from cloud_registry.ga4gh.registry.service import add_service_keys

# Logger of Connexion's request validation
VALIDATION_LOGGER = "connexion.decorators.validation"


def create_app() -> App:
    """Create the app from the config, with the error handler for validation
    errors (silencing Connexion's validation log messages), check admin
    groups, require a size limit of request bodies, and add missing keys of
    services. FOCA validates the service info (`service_info`) of the
    config, and limits the size of request bodies
    (`server.max_content_length`).

    Gunicorn calls this in each worker, after forking (MongoDB clients must
    not be shared across forks), so the checks run in each worker; if they
    fail, the worker fails to boot and Gunicorn exits.

    Returns:
        Connexion app.

    Raises:
        RuntimeError: Request bodies are not limited in size
            (`server.max_content_length` is null), no group is mapped to the
            role `admin` (unless admins are optional), or stored services
            are invalid or duplicates.
        pydantic.ValidationError: The config is invalid, e.g., the service
            info (naming the invalid field).
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
        # `null` would remove FOCA's size limit of request bodies, so that
        # bodies of any size are read before the access token is checked
        if get_config().server.max_content_length is None:
            raise RuntimeError(
                "Request bodies must be limited in size: set "
                "`server.max_content_length` (bytes), not null"
            )
        add_service_keys()
    return app


def main():
    """Create the app and run it with Flask's development server, on the
    configured port (for development; not for production).
    """
    app = create_app()
    app.run(port=app.port)


if __name__ == "__main__":
    main()
