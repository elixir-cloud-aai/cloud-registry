"""Entry point of the app (`python -m cloud_registry.app`)."""

from pathlib import Path

from connexion import App
from flask import current_app, request
from foca import Foca
from werkzeug.exceptions import RequestEntityTooLarge

from cloud_registry.ga4gh.registry.permissions import check_admin_groups
from cloud_registry.ga4gh.registry.service import add_service_keys
from cloud_registry.ga4gh.registry.service_info import check_service_info
from cloud_registry.utils import get_custom_config


def check_streamed_size() -> None:
    """Reject request bodies of unknown size over the size limit (`413`).

    For bodies without `Content-Length` (e.g., chunked), Werkzeug reads up to
    `MAX_CONTENT_LENGTH` bytes only and silently drops the rest, so one more
    byte is read here to tell whether the body is larger. The body is cached
    for the request, i.e., at most the limit plus one byte is kept in memory.

    Raises:
        werkzeug.exceptions.RequestEntityTooLarge: The body is larger than
            the size limit.
    """
    if request.content_length is not None:
        return  # checked by Werkzeug, by the declared size
    limit = current_app.config["MAX_CONTENT_LENGTH"]
    request.max_content_length = limit + 1
    # cached (`cache=True`, the default), so Connexion reads the same data
    if len(request.get_data()) > limit:
        raise RequestEntityTooLarge()


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
    # (`Content-Length`) are rejected without reading the body. Bodies of
    # unknown size are checked before each request, also before Connexion
    with app.app.app_context():
        limit = get_custom_config().max_content_length
    app.app.config["MAX_CONTENT_LENGTH"] = limit
    app.app.before_request(check_streamed_size)
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
