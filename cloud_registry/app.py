"""Entry point of the app (`python -m cloud_registry.app`)."""

from pathlib import Path

from connexion import App
from foca import Foca

from cloud_registry.ga4gh.registry.permissions import check_admin_groups
from cloud_registry.ga4gh.registry.service import add_service_keys
from cloud_registry.ga4gh.registry.service_info import check_service_info
from cloud_registry.utils import get_custom_config


def create_app() -> App:
    """Create the app from the config, with the size limit of request bodies.

    Returns:
        Connexion app.
    """
    foca = Foca(
        config_file=Path(__file__).parent / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()
    # Werkzeug rejects larger request bodies (`413`) when the request stream
    # is first accessed, i.e., when Connexion builds its request, before the
    # access token is checked and the body is parsed; declared sizes
    # (`Content-Length`) are rejected without reading the body, and bodies
    # of unknown size (chunked) are read up to the limit only
    with app.app.app_context():
        limit = get_custom_config().max_content_length
    app.app.config["MAX_CONTENT_LENGTH"] = limit
    return app


def main():
    """Create the app, check admin groups and service info, add missing keys
    of services, and run the app.
    """
    app = create_app()

    with app.app.app_context():
        check_admin_groups()
        check_service_info()
        add_service_keys()

    # start app
    app.run(port=app.port)


if __name__ == "__main__":
    main()
