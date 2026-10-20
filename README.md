# ELIXIR Cloud service registry

[![License][badge-license]][badge-url-license]
[![Docs][badge-docs]][badge-url-docs]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]

## Synopsis

GA4GH Service Registry API implementation for the ELIXIR Cloud. The registry is
currently deployed at [https://cloud-registry.rahtiapp.fi][deploy_url], but
will likely move soon.

## Description

Service entries comply with the [`ExternalService` schema][schema-service]
defined in the [GA4GH Service Registry API][ga4gh-registry].

For developers, an API documentation (of the code, not the exposed API) is
available [here][docs-api].

## Usage

Once [deployed](#Installation), the API will be served here:

```text
http://localhost:8080/ga4gh/registry/v1/
```

You can explore the service via the [Swagger UI][url-swagger].

> The app listens on the port set in the [config][config] (`server.port`);
> [`docker-compose.yaml`][compose] publishes it on the host. If you change
> either, update the URLs above accordingly.

[Endpoints][schema-endpoints] can be probed with `curl`, for example:

```bash
curl -X GET "http://localhost:8080/ga4gh/registry/v1/services" -H  "accept: application/json"
```

### Configuration

The app is configured by [`cloud_registry/config.yaml`][config]. To override
some of its values, write them to a config overlay file and list it in
environment variable `FOCA_CONFIG_OVERLAY` (several files separated by `:`);
FOCA merges overlays into the config on startup. The
[Helm chart][deployment-instructions] uses the same mechanism.

Each deployment must set two values that depend on it, otherwise the app
refuses to start:

- `security.auth.audience`: the client IDs of the apps whose LS Login tokens
  are accepted (e.g., the registry's front end);
- `security.access_control.group_mapping`: the group(s) whose members are
  admins (see [Access control](#access-control)), or
  `custom.access_control.require_admin: false` to run without admins.

Docker Compose mounts [`config.dev.yaml`][config-dev], an overlay for local
development that accepts tokens for any audience and requires no admins. To
override more values locally, put them in `config.local.yaml` and mount it
in a `docker-compose.override.yaml` (both are ignored by Git):

```yaml
services:
    cloud-registry:
        environment:
            FOCA_CONFIG_OVERLAY: /etc/cloud-registry/config.dev.yaml:/etc/cloud-registry/config.local.yaml
        volumes:
            - ./config.local.yaml:/etc/cloud-registry/config.local.yaml:ro
```

### Service info

On startup, the app registers the service info from `config.yaml`
(`custom.endpoints.service_info`) unless it registered the same config before.
Service info can also be replaced via `POST /service-info`, with the same or
another `id`. The most recently written service info is served, so posted
service info is kept on restarts unless the service info in the config
changes.

Public URLs, e.g., in the `Location` header of `POST /service-info`
responses, are built from `custom.endpoints.service` in `config.yaml`. Set
`api_path` to the path prefix in front of the API base path when the app is
served behind a reverse proxy under a subpath (e.g., `registry`), or leave it
empty. Previously, `api_path` held the API base path itself (e.g.,
`ga4gh/registry/v1`); remove that value when upgrading.

### Access control

Services and service info can be read by anyone. Writing requires an access
token issued by [LS Login][ls-login]:

- Any authenticated user can register a service (`POST /services`) and
  becomes its **owner**.
- Owners can update (`PUT`) and delete (`DELETE`) their services, and grant
  other users the roles `owner` or `maintainer` on them, by their LS Login
  user identifier (`sub` claim):

  ```bash
  curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/permissions/<user-id>" \
    -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
    -d '{"role": "maintainer"}'
  ```

  `GET /services/<id>/permissions` lists the roles granted on a service
  (with the users' names, once they have used the registry since being
  granted the role; names are deleted with the role), and
  `DELETE /services/<id>/permissions/<user-id>` revokes them. The last owner
  of a service can't be removed or downgraded (`409 Conflict`); delete the
  service instead.
- **Maintainers** can update, but not delete a service.
- Members of the groups that the deployment maps to the role `admin`
  (`security.access_control.group_mapping`) are **admins**: they can do
  everything, including setting the service info (`POST /service-info`) and
  registering services with chosen identifiers (`PUT` of a new service). For
  ELIXIR, that is the LS Login group `ECP_CLN:ADMIN`:

  ```yaml
  security:
      access_control:
          group_mapping:
              "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu": admin
  ```

  On startup, the app logs the groups mapped to `admin`.

`GET /me` returns the current user's identifier (to give to owners), whether
they are an admin, and their roles on services; it also stores their name for
these services. Owners are not shown in the
public service responses.

Access control is implemented with [FOCA][foca-access-control]; see the
`security` section of the [configuration][config].

## Installation

To quickly install the service for development/testing purposes, we recommend
deployment via [Docker Compose][docker-compose], as described below. For
more durable deployments on cloud native infrastructure, we also provide a
[Helm][helm] chart in the [`deployment/`][deployment] directory, together with
[basic deployment instructions][deployment-instructions] (details may need to
be adapted for your specific infrastructure).

### Requirements

The following software needs to be available on your system:

- [`git 2.17.1`][git]
- [`docker`][docker], with the [Compose plugin][docker-compose] (version 2)

> Where versions are indicated, they were used to test deployment. Other
> versions with the same major version numbers will likely work as well,
> especially newer ones.

### Deployment

First, clone the repository and traverse into the root directory with:

```bash
git clone git@github.com:elixir-cloud-aai/cloud-registry.git
cd cloud-registry
```

Then simply start up the service with:

```bash
docker compose up --build -d
```

MongoDB is only reachable from the app container; its data is kept in the
Compose project's `mongodb-data` volume (e.g., `cloud-registry_mongodb-data`,
see `docker volume ls`).

> Earlier versions kept the data in `../data/cloud-registry/db`. To keep using
> it, bind-mount it in a `docker-compose.override.yaml`:
>
> ```yaml
> services:
>     mongodb:
>         volumes:
>             - ../data/cloud-registry/db:/data/db
> ```

_**That't it!**_

You should now be able to use/explore the API as described in
the [usage section](#Usage).

#### Other useful commands

To shut down the service, run:

```bash
docker compose down
```

Add `--volumes` to also delete the database.

If you need to inspect the logs, call:

```bash
docker compose logs
```

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contribution
guidelines][contributing] if you are interested to contribute. Please mind the
[code of conduct][coc] for all interactions with the community.

To set up a development environment and run the checks that CI runs:

```bash
pip install -r requirements.txt -r requirements_dev.txt
pre-commit install  # run the checks on every commit
pre-commit run --all-files  # ruff, mypy, semgrep, ast-grep
pre-commit run --all-files --hook-stage manual gitleaks-all  # secrets
pytest
```

Run `pre-commit` in the environment with the dev requirements: the mypy and
ast-grep hooks use the installed packages. The first run sets up the other
hooks, which takes a few minutes.

The checks are configured in `.pre-commit-config.yaml` and `pyproject.toml`.
Project-specific rules are in `.semgrep/` (security) and `.ast-grep/`
(anti-patterns such as HTTP status codes as strings). To suppress a false
positive, add a comment with the reason to the line: `# noqa: <code>` (ruff),
`# nosemgrep: <rule-id>`, `# ast-grep-ignore: <rule-id>` or
`# gitleaks:allow`.

## Versioning

The project adopts the [semantic versioning][semver] scheme for versioning.
Currently the service is in beta stage, so the API may change without further
notice.

## License

This project is covered by the [Apache License 2.0][license-apache] also
[shipped with this repository][license].

## Contact

The project is a collaborative effort under the umbrella of [ELIXIR Cloud &
AAI][elixir-cloud]. Follow the link to get in touch with us via chat or email.
Please mention the name of this service for any inquiry, proposal, question
etc.

[badge-build-status]:<https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/checks.yaml/badge.svg>
[badge-coverage]:<https://img.shields.io/coveralls/github/elixir-cloud-aai/cloud-registry>
[badge-docs]: <https://readthedocs.org/projects/cloud-registry/badge/?version=latest>
[badge-github-tag]:<https://img.shields.io/github/v/tag/elixir-cloud-aai/cloud-registry?color=C39BD3>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]:<https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/checks.yaml>
[badge-url-coverage]:<https://coveralls.io/github/elixir-cloud-aai/cloud-registry>
[badge-url-docs]: <https://cloud-registry.readthedocs.io/en/latest/?badge=latest>
[badge-url-github-tag]:<https://github.com/elixir-cloud-aai/cloud-registry/releases>
[badge-url-license]:<http://www.apache.org/licenses/LICENSE-2.0>
[ga4gh-registry]: <https://github.com/ga4gh-discovery/ga4gh-service-registry>
[compose]: docker-compose.yaml
[config]: cloud_registry/config.yaml
[config-dev]: config.dev.yaml
[deploy_url]: <https://cloud-registry.rahtiapp.fi/ga4gh/registry/v1/ui>
[deployment]: deployment/
[deployment-instructions]: deployment/README.md
[docker]: <https://docs.docker.com/get-docker/>
[docker-compose]: <https://docs.docker.com/compose/install/>
[docs-api]: <https://cloud-registry.readthedocs.io/en/latest/>
[git]: <https://git-scm.com/book/en/v2/Getting-Started-Installing-Git>
[helm]: <https://helm.sh/>
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[semver]: <https://semver.org/>
[schema-service]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158>
[schema-endpoints]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L16>
[url-swagger]: <http://localhost:8080/ga4gh/registry/v1/ui>
[foca-access-control]: https://foca.readthedocs.io/en/latest/access-control/
[ls-login]: https://lifescience-ri.eu/ls-login/
