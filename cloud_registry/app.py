"""Entry point of the app (`python -m cloud_registry.app`)."""

from pathlib import Path

from foca import Foca

from cloud_registry.ga4gh.registry.permissions import check_admin_groups
from cloud_registry.ga4gh.registry.service_info import check_service_info


def main():
    """Create the app, check admin groups and service info, and run the app."""
    foca = Foca(
        config_file=Path(__file__).parent / "config.yaml",
        custom_config_model="cloud_registry.service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()

    # check admin groups and service info
    with app.app.app_context():
        check_admin_groups()
        check_service_info()

    # start app
    app.run(port=app.port)


if __name__ == "__main__":
    main()
