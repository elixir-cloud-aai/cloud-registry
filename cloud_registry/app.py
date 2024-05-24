from pathlib import Path
import logging
from foca import Foca
from foca.security.auth import validate_token  # noqa: F401

from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo

logger = logging.getLogger(__name__)


def main():
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        raise FileNotFoundError(f"Config file not found: {config_path}")
    # create app object
    foca = Foca(
        config_file=config_path,
        custom_config_model="service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()

    # register service info
    with app.app.app_context():
        service_info = RegisterServiceInfo()
        service_info.set_service_info_from_config()

    # start app
    app.run(port=app.port)


if __name__ == "__main__":
    main()
