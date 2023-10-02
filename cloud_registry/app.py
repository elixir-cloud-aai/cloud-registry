from foca import Foca
from foca.security.auth import validate_token  # noqa: F401

from cloud_registry.ga4gh.registry.service_info import RegisterServiceInfo


def main():
    # create app object
    foca = Foca(
        config_file="config.yaml",
        custom_config_model="service_models.custom_config.CustomConfig",
    )
    app = foca.create_app()

    # register service info
    with app.app.app_context():
        service_info = RegisterServiceInfo()
        service_info.set_service_info_from_config()

    # start app
    app.run(port=app.port)


if __name__ == '__main__':
    main()
