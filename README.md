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

Registering a service (`POST /services`, or `PUT /services/{serviceId}` with a
new identifier) returns `201 Created`, with the identifier of the service in
the body and its [public URL](#public-urls) in the `Location` header; both
returned `200 OK` before. Updating a service (`PUT`) returns `200 OK`, as
before. Identifiers `.` and `..` are rejected (`400 Bad Request`); services
registered with them before can still be read and deleted, but must be
registered again under other identifiers to be updated.

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

#### Public URLs

Public URLs, e.g., in `Location` headers, are built from
`custom.endpoints.service` (scheme, host and port). The Helm chart sets them
from its `host_name` value; other deployments must set them, as the defaults
(`https://0.0.0.0:8080`) aren't reachable (the Docker Compose setup sets
`http://localhost:8080` in [`config.dev.yaml`][config-dev]). Set `api_path` to
the path prefix in front of the API base path when the app is served behind a
reverse proxy under a subpath (e.g., `registry`), or leave it empty.
Previously, `api_path` held the API base path itself (e.g.,
`ga4gh/registry/v1`); remove that value when upgrading.

### Service info

`GET /service-info` serves the service info from the config
(`custom.endpoints.service_info`), without the database. On startup, the app
checks it against the GA4GH `Service` schema and refuses to start if it is
invalid, naming the invalid field. `version` defaults to the version of the
installed app; `createdAt` and `updatedAt` are served only if set, and must be
RFC 3339 date-times (e.g., `2020-11-08T00:00:00Z`). Fields and list items set
to `null` (e.g., in a config overlay) are not served. To change the service
info, change the config and restart the app (the Helm chart restarts it when
the `config` value changes).

Previously, service info could be set via `POST /service-info` and was stored
in the database. That endpoint is gone, and stored service info is no longer
served (the `service_info` collection can be dropped): put it in the config
when upgrading.

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
  everything, including registering services with chosen identifiers (`PUT`
  of a new service) and [verifying services](#verified-services). For
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

### Duplicate services

A service is identified by its base URL (`url`) and its API type (`type.group`
and `type.artifact`, case-insensitive, but not `type.version`, so that
upgrading a service to a new API version updates its entry). URLs are compared
after normalizing common spellings: lowercase schemes and hosts, encoded
international host names (IDNA 2008), canonical IPv6 addresses, percent-encoded
non-ASCII characters in paths, and without trailing dots of hosts, default
ports, dot segments (`/./`, `/../`), repeated and trailing slashes, needless
percent-encoding in paths, queries and fragments. URLs with other schemes
(e.g., `http` and `https`) are different services. Invalid URLs, URLs without
scheme or host, and URLs with user info (`user:password@`) are rejected (`400
Bad Request`, saying why).

Registering a service again (`POST /services`), or changing a service to the
URL and API type of another one (`PUT`), returns `409 Conflict`, with the
identifier of the registered service in `serviceId`, so that users can
contact its owner or an admin.

As anyone logged in can register a URL first, including one of a deployment
they don't operate, a squatter could block the real operator of a service.
Admins resolve such cases: they can delete the entry, or grant the real
operator the role `owner` on it and revoke the squatter's role. Consumers that
need trustworthy entries should rely on entries checked by admins (see
[Verified services](#verified-services)). Proving control of a
URL (e.g., by fetching `{url}/service-info` on registration) was rejected:
the identifier in service info is self-declared, and fetching user-supplied
URLs would allow server-side request forgery.

Services stored before this check get their key on startup. If some of them
have invalid URLs, or the same URL and API type as another one, the app
refuses to start and names all of them. Fix or delete them in the database,
e.g., with `mongosh` (see the [deployment instructions][deployment-instructions]
for access to the database of a Helm deployment):

```javascript
db.getSiblingDB("serviceStore").services.deleteOne({id: "<id>"})
db.getSiblingDB("serviceStore").services.updateOne({id: "<id>"}, {$set: {url: "<URL>"}})
```

Then start the app again; it adds the missing keys.

### Verified services

Admins mark services as **verified** once they have checked them, e.g., that
a service accepts LS Login tokens. What admins check is up to the
deployment; the registry only records when a service was verified. Owners
can't verify their own services.

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/verification" \
  -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"url": "https://tes.example.org/ga4gh/tes/v1", "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}}'
```

The body names the URL and API type that the admin checked. It is optional,
but recommended: the service is only verified if it still has exactly that
URL (as registered, character by character) and the same API type
(`type.group` and `type.artifact`, case-insensitive; `type.version` is not
compared), checked in the same write. Otherwise, e.g., if the owner changed
the URL after the admin checked it, the request returns `409 Conflict` and
changes nothing. Invalid URLs, and bodies that aren't JSON (e.g., form
data), return `400 Bad Request`. Without a body (or with `null`), the
service is verified as it is registered at the time of the request.

The request returns the verification, with the URL and API type of the
service:

```json
{"url": "https://tes.example.org/ga4gh/tes/v1", "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}, "verified_at": "2026-10-20T12:34:56.789Z"}
```

Verifying a service again renews it. `DELETE` on the same path removes it.
Both are for admins only, and return `404 Not Found` for unknown services
(`DELETE` also for unverified ones).

Verifications can be read by anyone: `GET /services/<id>/verification`
returns the verification of a service (`404 Not Found` if it is not
verified), and `GET /verifications` lists all verified services, sorted by
identifier, each with its identifier as `service_id` and the fields above.

The GA4GH endpoints are unchanged: `GET /services` and
`GET /services/<id>` don't show verifications. Clients that should only use
verified services (e.g., a [proTES][protes] gateway choosing TES instances)
list the services (`GET /services`) and keep those listed by
`GET /verifications` with the same identifier, `url` and `type` (`group` and
`artifact`), so that a service changed between the two requests isn't used.

A verification applies to the service as it was when it was verified:

- **URL, as written:** updating the service with any other URL removes the
  verification, even another spelling of the same one (e.g., another case
  of the host, a trailing slash, or a query or fragment, which are ignored
  when [comparing services](#duplicate-services)).
- **API type:** updating `type.group` or `type.artifact` (compared as for
  duplicates) removes it.
- Changing them back doesn't restore it. Other updates (e.g., of `version`,
  `type.version` or `name`) keep it.
- Deleting a service deletes its verification.
- Admins should send the URL and API type they checked when verifying (see
  above), so that a service changed meanwhile isn't verified.

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

## Versioning and releases

The project adopts the [semantic versioning][semver] scheme for versioning.
Currently the service is in beta stage, so the API may change without further
notice.

The version is only stored in Git tags (`vX.Y.Z`): the package version is
derived from them (via [setuptools-scm][setuptools-scm]), and the service info
reports it. Builds without Git metadata, such as local Docker builds (e.g.,
with Docker Compose), report `0.0.0.dev0`; the workflows pass the version to
the image build. Images on Docker Hub (`elixircloud/cloud-registry`) are
tagged:

- `X.Y.Z`: release `X.Y.Z`; `latest`: the highest release on `main`;
- `dev`: the latest commit on `dev`; `sha-<commit>`: commit `<commit>` on
  `dev` (full SHA), to deploy a fixed commit.

Features and fixes are merged into `dev`. Branch `main` only ever moves
forward to commits on `dev` that are released. To release version `X.Y.Z`
from commit `<sha>` on `dev`, after CI passed for it:

```bash
git push origin <sha>:main  # fails unless it is a fast-forward
git tag -s vX.Y.Z <sha> -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag must be annotated and signed (`git tag -s`), with a key that GitHub
can verify (added to the tagger's GitHub account). The [release
workflow][release-workflow], triggered by the tag, checks that GitHub
verified the signature and that the tag's commit is on `main`. It packages
the Helm chart with the release version as `version` and `appVersion` (the
default image tag). After approval in the `release` environment, it builds
the image, checks that the app reports the release version, pushes exactly
that image tagged `X.Y.Z` (and `latest`, if it is the highest release on
`main`), and creates a GitHub release with generated notes and the chart
(`.tgz`). No packages are published to PyPI. Tags of pre-releases (e.g.,
`vX.Y.Zrc1`, also `aN` and `bN`) create GitHub pre-releases, with chart
version `X.Y.Z-rc1`, and never update `latest`. Images of commits on `dev`
are published by the [CI workflow][checks-workflow] (environment
`dev-images`).

Maintainers: the workflows rely on these repository settings: environment
`dev-images` limited to branch `dev`; environment `release` with a required
reviewer; a tag ruleset for `v*` that restricts who may create them and
prevents moving or deleting them; `main` protected against non-fast-forward
pushes; and the Docker Hub secrets (`DOCKERHUB_LOGIN`, `DOCKERHUB_TOKEN`,
`DOCKERHUB_ORG`) only in the two environments, not as repository secrets.

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
[checks-workflow]: .github/workflows/checks.yaml
[release-workflow]: .github/workflows/release.yaml
[semver]: <https://semver.org/>
[setuptools-scm]: <https://setuptools-scm.readthedocs.io/>
[schema-service]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158>
[schema-endpoints]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L16>
[url-swagger]: <http://localhost:8080/ga4gh/registry/v1/ui>
[foca-access-control]: https://foca.readthedocs.io/en/latest/access-control/
[ls-login]: https://lifescience-ri.eu/ls-login/
[protes]: https://github.com/elixir-cloud-aai/proTES
