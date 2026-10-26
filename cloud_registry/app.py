"""App factory, and entry point for development.

The image serves the app with Gunicorn (`cloud_registry.app:create_app()`, with
FOCA's settings, `foca.gunicorn_config`); `python -m cloud_registry.app` runs it
with Flask's development server.
"""

import logging
from pathlib import Path

from connexion import App
from foca import Foca, get_config
from foca.security.auth import require_no_service_clients

from cloud_registry.ga4gh.registry.service import (
    add_service_keys,
    warn_environments,
    warn_invalid_urls,
    warn_url_schemes,
)
from cloud_registry.ga4gh.registry.service_types import (
    add_service_types,
    sync_pending_services,
)

logger = logging.getLogger(__name__)


def create_app() -> App:
    """Create the app from the config, with the registry's checks.

    Refuses service clients (FOCA's `require_no_service_clients()`), as the
    app doesn't support them, requires a size limit of request bodies, adds
    missing keys of services and the types of registered services that
    aren't stored (e.g., on upgrades), and warns of stored services with
    invalid URLs or URL schemes that are not allowed. FOCA validates the
    config, e.g., that token issuers are trusted (needed for access control)
    with an audience, the group settings and the service info
    (`service_info`), refuses to start without admins
    (`security.access_control.require_admin`), warns of placeholders in the
    service info, and limits the size of request bodies
    (`server.max_content_length`). Also requires the public URL of the app
    (`server.public_url`), for the `Location` header of new services.

    Gunicorn calls this in each worker, after forking (MongoDB clients must
    not be shared across forks), so the checks run in each worker; if they
    fail, the worker fails to boot and Gunicorn exits.

    Returns:
        Connexion app.

    Raises:
        RuntimeError: Request bodies are not limited in size
            (`server.max_content_length` is null), the public URL
            (`server.public_url`) is not set, service clients are
            configured (which the app doesn't support), or stored services
            are invalid or duplicates.
        pydantic.ValidationError: The config is invalid, e.g., no token
            issuers are trusted, or the service info is invalid (naming the
            invalid field).
        ValueError: The config is invalid, e.g., groups are mapped without
            a group claim, there are no admins (unless admins are optional,
            `security.access_control.require_admin`), or the custom config
            (`custom`) still sets the service info
            (`custom.endpoints.service_info`) or the size limit of request
            bodies (`custom.max_content_length`).
    """
    foca = Foca(
        config_file=Path(__file__).parent / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()
    require_no_service_clients(app.app)

    with app.app.app_context():
        # `null` would remove FOCA's size limit of request bodies, so that
        # bodies of any size are read before the access token is checked
        if get_config().server.max_content_length is None:
            raise RuntimeError(
                "Request bodies must be limited in size: set "
                "`server.max_content_length` (bytes), not null"
            )
        # FOCA only raises on the first request that needs it (the
        # `Location` header of `POST /services`)
        if get_config().server.public_url is None:
            raise RuntimeError(
                "The public URL of the app must be set: set "
                "`server.public_url` to its URL as clients see it (e.g., "
                "`https://registry.example.org/`)"
            )
        add_service_keys()
        add_service_types()
        sync_pending_services()
        warn_invalid_urls()
        warn_url_schemes()
        warn_environments()
    return app


def main() -> None:
    """Run the app with Flask's development server, on the configured port.

    For development; not for production.
    """
    app = create_app()
    app.run(port=app.port)


if __name__ == "__main__":
    main()
