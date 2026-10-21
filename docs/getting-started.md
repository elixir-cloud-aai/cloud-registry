# Getting started

To quickly install the service for development/testing purposes, we recommend
deployment via [Docker Compose](https://docs.docker.com/compose/install/), as
described below. For more durable deployments on cloud native infrastructure,
we also provide a [Helm](https://helm.sh/) chart in the
[`deployment/`](https://github.com/elixir-cloud-aai/cloud-registry/tree/dev/deployment)
directory (see [Deployment](deployment.md)). The [FOCA deployment
guide](https://foca.readthedocs.io/en/latest/deployment/) explains how FOCA
apps, such as this one, are configured with Docker Compose and Helm (base
config, overlays and secrets).

## Quick start

--8<-- "README.md:quick-start"

!!! note
    Where versions are indicated, they were used to test deployment. Other
    versions with the same major version numbers will likely work as well,
    especially newer ones.

MongoDB is only reachable from the app container; its data is kept in the
Compose project's `mongodb-data` volume (e.g., `cloud-registry_mongodb-data`,
see `docker volume ls`). Earlier versions kept the data elsewhere (see
[Upgrading](upgrading.md#docker-compose-data)).

### Other useful commands

To shut down the service, run:

```bash
docker compose down
```

Add `--volumes` to also delete the database.

If you need to inspect the logs, call:

```bash
docker compose logs
```

## Usage

!!! note
    The image's Gunicorn listens on port 8080 (`--bind`, see
    [Server](configuration.md#server));
    [`docker-compose.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/docker-compose.yaml)
    publishes it on the host's loopback interface (`127.0.0.1`) only, as its
    development config accepts tokens for any audience. If you change the
    published port, update the URLs above accordingly. `server.port` of the
    [config](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml)
    only sets the port of the development server
    (`python -m cloud_registry.app`).

[Endpoints](https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L16)
can be probed with `curl`, as above. Requests that change data need an access
token (see [Authentication](access-control.md#authentication)). See
[API](api.md) for all endpoints, and [Configuration](configuration.md#local-overrides)
to change the config of the Docker Compose setup.
