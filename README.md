# ELIXIR Cloud service registry

[![License][badge-license]][badge-url-license]
[![Docs][badge-docs]][badge-url-docs]
[![Build_status][badge-build-status]][badge-url-build-status]
[![Coverage][badge-coverage]][badge-url-coverage]

## Synopsis

GA4GH Service Registry API implementation for the ELIXIR Cloud. The ELIXIR
Cloud registry, with its [web front end][cloud-registry-ui], is deployed at
[https://registry.elixir-on.cloud/][deploy_url].

## Contents

- [Description](#description)
- [Installation](#installation)
- [Usage](#usage)
- [API](#api)
- [Authentication](#authentication)
- [Access control](#access-control)
- [Service info](#service-info)
- [Duplicate services](#duplicate-services)
- [Verified services](#verified-services)
- [Configuration](#configuration)
- [Development](#development)
- [Contributing](#contributing)
- [Versioning and releases](#versioning-and-releases)
- [License](#license)
- [Contact](#contact)

## Description

Service entries comply with the [`ExternalService` schema][schema-service]
defined in the [GA4GH Service Registry API][ga4gh-registry]. Anyone can read
them; users logged in with [LS Login][ls-login] can register services and
manage those they own (see [Access control](#access-control)).

For developers, an API documentation (of the code, not the exposed API) is
available [here][docs-api].

## Installation

To quickly install the service for development/testing purposes, we recommend
deployment via [Docker Compose][docker-compose], as described below. For
more durable deployments on cloud native infrastructure, we also provide a
[Helm][helm] chart in the [`deployment/`][deployment] directory (see
[Kubernetes](#kubernetes)).

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
git clone https://github.com/elixir-cloud-aai/cloud-registry.git
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

_**That's it!**_

You should now be able to use/explore the API as described in
the [usage section](#usage).

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

### Kubernetes

The [Helm chart][deployment] comes with [deployment
instructions][deployment-instructions]: configuration via values files, image
tags, installation, ingress, upgrades and MongoDB (details may need to be
adapted for your specific infrastructure). The [FOCA deployment
guide][foca-deployment] explains how FOCA apps, such as this one, are
configured with Docker Compose and Helm (base config, overlays and secrets).

## Usage

Once [deployed](#installation), the API will be served here:

```text
http://localhost:8080/ga4gh/registry/v1/
```

You can explore the service via the [Swagger UI][url-swagger], which also
serves the OpenAPI specification (`/ga4gh/registry/v1/openapi.json`).

> The image's Gunicorn listens on port 8080 (`--bind`, see [Server](#server));
> [`docker-compose.yaml`][compose] publishes it on the host. If you change
> the published port, update the URLs above accordingly. `server.port` of the
> [config][config] only sets the port of the development server
> (`python -m cloud_registry.app`).

[Endpoints][schema-endpoints] can be probed with `curl`, for example:

```bash
curl -X GET "http://localhost:8080/ga4gh/registry/v1/services" -H  "accept: application/json"
```

Requests that change data need an access token (see
[Authentication](#authentication)).

## API

The endpoints of the [GA4GH Service Registry API][ga4gh-registry] (version
1.0.0) are unchanged: the registry does not extend their requests or
responses. Everything else is added as extra operations and endpoints, all
under the API base path `/ga4gh/registry/v1`:

| Endpoint | Methods | Who |
| --- | --- | --- |
| `/service-info` | `GET` (GA4GH) | anyone |
| `/services` | `GET` (GA4GH) | anyone |
| `/services` | `POST` | any authenticated user |
| `/services/types` | `GET` (GA4GH) | anyone |
| `/services/{serviceId}` | `GET` (GA4GH) | anyone |
| `/services/{serviceId}` | `PUT` | owners, maintainers, admins (new identifiers: admins) |
| `/services/{serviceId}` | `DELETE` | owners, admins |
| `/services/{serviceId}/permissions` | `GET` | owners, admins |
| `/services/{serviceId}/permissions/{userId}` | `PUT`, `DELETE` | owners, admins |
| `/services/{serviceId}/verification` | `GET` | anyone |
| `/services/{serviceId}/verification` | `PUT`, `DELETE` | admins |
| `/verifications` | `GET` | anyone |
| `/me` | `GET` | any authenticated user |

Registering a service (`POST /services`, or `PUT /services/{serviceId}` with a
new identifier) returns `201 Created`, with the identifier of the service in
the body and its [public URL](#public-urls) in the `Location` header; updating
a service (`PUT`) returns `200 OK`. Only admins can register services with
`PUT`: for others, unknown identifiers return `404 Not Found`, also if the
service is deleted while it is updated. Identifiers `.` and `..` (which
clients resolve in URLs) and `types` (the path of `GET /services/types`) are
rejected (`400 Bad Request`).

URLs in registrations must be HTTP(S) URLs with a host: the service `url`,
`documentationUrl`, `organization.url` and `contactUrl`, which can also be an
email address (`mailto:`); other URLs are rejected (`400 Bad Request`, saying
why). Links are stored as written, so they must not contain whitespace (unlike
service URLs, which may have leading and trailing spaces), control or format
characters (except joiners in international host names), and no URLs may
contain malformed percent-encodings. Anyone logged in can register services, so
clients should treat registry content as untrusted: escape it rather than
render it as HTML, and don't follow or fetch its URLs blindly (e.g., to
internal addresses).

Timestamps in registrations (`createdAt`, `updatedAt`) must be RFC 3339
date-times (e.g., `2019-06-04T12:58:19Z`); others are rejected (`400 Bad
Request`). Services stored before this check may have other values; they
can't be updated unchanged, only with valid timestamps (or without them).

Request bodies are limited to 1 MiB by default (`413 Content Too Large`; see
[Request size limit](#request-size-limit)); the number of registrations per
user is not limited, so deployments should limit request rates at the ingress
(see the [deployment instructions][deployment-instructions]). Strings in the bodies of
registrations (`POST /services`, `PUT /services/{serviceId}`) and
verifications (`PUT /services/{serviceId}/verification`) are limited in length
(longer ones are rejected with `400 Bad Request`):

| Fields | Maximum length (characters) |
| --- | --- |
| URLs: `url`, `organization.url`, `contactUrl`, `documentationUrl` | 2048 |
| Names: `name`, `organization.name` | 1024 |
| `description` | 4096 |
| `version`, `environment`, `type.group`, `type.artifact`, `type.version` | 256 |

Services stored before these limits were introduced may have longer values;
they can't be updated unchanged, only with the values shortened.

Requests that don't match the [API specification](#usage) (e.g., missing or
unknown fields, values that are too long, extra query parameters, or bodies
that aren't JSON) are rejected with `400 Bad Request`, with the reason in
`detail`.

See [Access control](#access-control) for the roles and [Verified
services](#verified-services) for verifications. Extensions of the GA4GH
endpoints, such as pagination, search or filters, will be reconsidered with
the upcoming new version of the Service Registry specification.

## Authentication

Read operations (`GET`, except `/services/{serviceId}/permissions` and `/me`,
see the [table above](#api)) are public. All other operations require an
access token issued by [LS Login][ls-login], sent as a bearer token:

```bash
curl -X POST "http://localhost:8080/ga4gh/registry/v1/services" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d @service.json
```

Requests without a valid token are rejected with `401 Unauthorized`;
requests with a valid token, but without the required role, with `403
Forbidden`.

Tokens are obtained by logging in with LS Login through an app registered as
an LS Login client (OpenID Connect), e.g., the registry's [front
end][cloud-registry-ui], or a client of your own for scripts. The registry
accepts tokens only:

- of the issuers in `security.auth.issuers` (LS Login by default);
- for the client IDs in `security.auth.audience` (the `aud` claim), which each
  deployment sets to the apps that call it (see
  [Configuration](#configuration)); the Docker Compose setup accepts tokens
  for any audience.

The registry reads the user's group memberships (`eduperson_entitlement`,
needed for the [admin role](#access-control)) and name from LS Login's user
info endpoint with the token, so the token must be allowed to release them
(e.g., request the corresponding scopes when logging in). Tokens are validated
by [FOCA][foca-auth].

## Access control

Services and service info can be read by anyone. Writing requires an access
token issued by [LS Login][ls-login] (see [Authentication](#authentication)):

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

For auditing, the app logs (at level `INFO`) who changed which roles of whom
on which service, who deleted services, and who verified services or removed
their verifications (also by updating their URL or API type). Changes via the
[permissions API](#permissions-api) are logged by FOCA's request logging only.

### Permissions API

FOCA also serves its permissions API at `/admin/access-control/permissions`
(not part of the registry's API specification), which lists, adds, changes and
deletes the stored policy rules directly. Only admins can use it. Use the
registry's endpoints above to manage roles on services: the permissions API
bypasses the registry's checks, e.g., it can remove the last owner of a service
(without `409 Conflict`) and leaves the stored names of users (see
`GET /services/{serviceId}/permissions`) in place. The configured policies
(`security.access_control.policies`) are managed on startup and can't be
changed via the API.

Admins can make other users admins with it, by granting them the role `admin`
(a `g` rule for the user identifier with the prefix `user:`); `GET /me`
reports them as admins, too:

```bash
curl -X POST "http://localhost:8080/admin/access-control/permissions" \
  -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"policy_type": "g", "rule": {"v0": "user:<user-id>", "v1": "admin"}}'
```

The request returns the identifier of the rule; `DELETE` on
`/admin/access-control/permissions/<rule-id>` revokes it. Prefer the group
mapping for admins, as it is configured with the deployment; roles granted via
the API are stored in the database. The API is served like the registry's
endpoints (e.g., the Helm chart's Ingress routes all paths); to keep it off
the internet, route only `/ga4gh/registry/v1/` through a reverse proxy.

## Service info

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

## Duplicate services

A service is identified by its base URL (`url`) and its API type (`type.group`
and `type.artifact`, case-insensitive, but not `type.version`, so that
upgrading a service to a new API version updates its entry). URLs are compared
after normalizing common spellings: lowercase schemes and hosts, encoded
international host names (IDNA 2008), canonical IPv6 addresses, percent-encoded
non-ASCII characters in paths, and without trailing dots of hosts, default
ports, dot segments (`/./`, `/../`), repeated and trailing slashes, needless
percent-encoding in paths, queries and fragments. `http` and `https` URLs are
different services. Invalid URLs, URLs without scheme or host, with schemes
other than `http` and `https`, user info (`user:password@`), whitespace (other
than leading and trailing spaces), control or format characters are rejected
(`400 Bad Request`, saying why).

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

Services stored before this check get their key on startup; only services
without a key are checked again. If some of them have invalid URLs, or the same
URL and API type as another one, the app refuses to start and names all of
them. Invalid URLs include some that were accepted before: URLs with schemes
other than `http` and `https`, whitespace or control characters, malformed
percent-encodings (also in queries and fragments), or port 0. Fix or delete
them in the database, e.g., with `mongosh` (see the [deployment
instructions][deployment-instructions] for access to the database of a Helm
deployment):

```javascript
db.getSiblingDB("serviceStore").services.deleteOne({id: "<id>"})
db.getSiblingDB("serviceStore").services.updateOne({id: "<id>"}, {$set: {url: "<URL>"}})
```

Then start the app again; it adds the missing keys.

## Verified services

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

## Configuration

The app is configured by [`cloud_registry/config.yaml`][config], the base
config, which documents its values. To override some of them, write them to a
config overlay file and list it in environment variable `FOCA_CONFIG_OVERLAY`
(several files separated by `:`); FOCA merges overlays into the config on
startup (see the [FOCA deployment guide][foca-deployment]). The [Helm
chart][deployment-instructions] uses the same mechanism: it renders its
`config` value (in a values file per deployment) into an overlay. Overlays
cannot change the address the image listens on: `server.host` and
`server.port` apply to the development server only, not to Gunicorn (see
[Server](#server)).

Each deployment must set two values that depend on it, otherwise the app
refuses to start:

- `security.auth.audience`: the client IDs of the apps whose LS Login tokens
  are accepted (e.g., the registry's front end; see
  [Authentication](#authentication));
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

### Public URLs

Public URLs, e.g., in `Location` headers, are built from
`custom.endpoints.service` (scheme, host and port). The Helm chart sets them
from its `host_name` value; other deployments must set them, as the defaults
(`https://0.0.0.0:8080`) aren't reachable (the Docker Compose setup sets
`http://localhost:8080` in [`config.dev.yaml`][config-dev]). Set `api_path` to
the path prefix in front of the API base path when the app is served behind a
reverse proxy under a subpath (e.g., `registry`), or leave it empty.
Previously, `api_path` held the API base path itself (e.g.,
`ga4gh/registry/v1`); remove that value when upgrading.

### Request size limit

Request bodies are limited to `custom.max_content_length` bytes (default:
1048576, i.e., 1 MiB). Larger requests are rejected with `413 Content Too
Large` before the access token is checked and the body is parsed: requests
whose `Content-Length` exceeds the limit are rejected without reading the
body. Bodies of unknown size (`Transfer-Encoding: chunked`) are read up to one
byte over the limit, and rejected if they are larger.

Set the same limit at the ingress or reverse proxy in front of the app, so
that it rejects larger requests itself; the Helm chart sets the limit of
ingress-nginx from `custom.max_content_length` (see the [deployment
instructions][deployment-instructions]).

### Server

The image serves the app with [Gunicorn][gunicorn] on port 8080, with
settings for a small container in
[`cloud_registry/gunicorn_config.py`][gunicorn-config]: one worker process
with 4 threads, workers restarted if their main loop doesn't respond for 30
seconds (requests run in threads, so slow requests don't count), 20 seconds
to finish requests on shutdown, idle keep-alive connections closed after 75
seconds, and Gunicorn's size limits of the request line and headers. Keep the
keep-alive timeout longer than that of a reverse proxy in front of the app
for its connections to the app (60 seconds for ingress-nginx): otherwise, the
proxy may reuse a connection just as Gunicorn closes it, and the request
fails (`502`). To change the settings, set `GUNICORN_CMD_ARGS` to [Gunicorn
options][gunicorn-settings] (e.g., `--workers 2 --threads 8`); Docker Compose
passes it on from the host, and the Helm chart sets it from its
`cloud_registry.gunicorn` values.

Each worker loads the whole app (about 55 MiB of memory), with its own
database client, and runs the startup checks (e.g., of the service info and
the admin groups); if they fail, Gunicorn exits. Gunicorn always listens on
`0.0.0.0:8080` (its `bind` setting): `server.host` and `server.port` of the
config apply only to Flask's development server
(`python -m cloud_registry.app`), which is not for production, and changing
them does not move the image's traffic.

Serve the app behind a reverse proxy that buffers requests (e.g.,
ingress-nginx, as with the Helm chart): Gunicorn's threads read requests
themselves, so slow clients can tie them up.

## Development

Development needs Python 3.10 or later, and [Helm][helm] for the tests of the
chart (skipped without it). To set up a development environment (e.g., in a
virtual environment) and run the checks that CI runs:

```bash
pip install -r requirements.txt -r requirements_dev.txt
pip install -e .  # the app itself, e.g., for its version
pre-commit install  # run the checks on every commit
pre-commit run --all-files  # ruff, mypy, semgrep, ast-grep
pre-commit run --all-files --hook-stage manual gitleaks-all  # secrets
pytest
```

The tests need no database (they use `mongomock`). To try changes in the
running app, rebuild it with `docker compose up --build -d` (see
[Deployment](#deployment)). `python -m cloud_registry.app` runs the app with
Flask's development server instead of Gunicorn (see [Server](#server)); it
needs a MongoDB at `db.host` of the config.

Run `pre-commit` in the environment with the dev requirements: the mypy and
ast-grep hooks use the installed packages. The first run sets up the other
hooks, which takes a few minutes.

The checks are configured in [`.pre-commit-config.yaml`][pre-commit-config]
and [`pyproject.toml`][pyproject]. Project-specific rules are in `.semgrep/`
(security) and `.ast-grep/` (anti-patterns such as HTTP status codes as
strings). To suppress a false positive, add a comment with the reason to the
line: `# noqa: <code>` (ruff), `# nosemgrep: <rule-id>`,
`# ast-grep-ignore: <rule-id>` or `# gitleaks:allow`.

The [code documentation][docs-api] is built by Read the Docs (configured in
[`.readthedocs.yaml`][readthedocs-config]); warnings fail the build. To build
it locally, with the dev environment set up as above:

```bash
pip install -r docs/api/requirements.txt
cd docs/api
sphinx-build -W --keep-going -b html . ../_build/html  # ignored by Git
```

## Contributing

This project is a community effort and lives off your contributions, be it in
the form of bug reports, feature requests, discussions, or fixes and other code
changes. Please refer to our organization's [contribution
guidelines][contributing] if you are interested to contribute. Please mind the
[code of conduct][coc] for all interactions with the community. See
[Development](#development) to set up a development environment.

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
[badge-coverage]:<https://codecov.io/gh/elixir-cloud-aai/cloud-registry/branch/dev/graph/badge.svg>
[badge-docs]: <https://readthedocs.org/projects/cloud-registry/badge/?version=latest>
[badge-license]:<https://img.shields.io/badge/license-Apache%202.0-blue.svg>
[badge-url-build-status]:<https://github.com/elixir-cloud-aai/cloud-registry/actions/workflows/checks.yaml>
[badge-url-coverage]:<https://codecov.io/gh/elixir-cloud-aai/cloud-registry>
[badge-url-docs]: <https://cloud-registry.readthedocs.io/en/latest/?badge=latest>
[badge-url-license]:<https://www.apache.org/licenses/LICENSE-2.0>
[ga4gh-registry]: <https://github.com/ga4gh-discovery/ga4gh-service-registry>
[cloud-registry-ui]: <https://github.com/elixir-cloud-aai/cloud-registry-ui>
[compose]: docker-compose.yaml
[config]: cloud_registry/config.yaml
[config-dev]: config.dev.yaml
[deploy_url]: <https://registry.elixir-on.cloud/>
[deployment]: deployment/
[deployment-instructions]: deployment/README.md
[docker]: <https://docs.docker.com/get-docker/>
[docker-compose]: <https://docs.docker.com/compose/install/>
[docs-api]: <https://cloud-registry.readthedocs.io/en/latest/>
[git]: <https://git-scm.com/book/en/v2/Getting-Started-Installing-Git>
[gunicorn]: <https://gunicorn.org/>
[gunicorn-config]: cloud_registry/gunicorn_config.py
[gunicorn-settings]: <https://gunicorn.org/reference/settings/>
[helm]: <https://helm.sh/>
[license]: LICENSE
[license-apache]: <https://www.apache.org/licenses/LICENSE-2.0>
[elixir-cloud]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai>
[coc]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CODE_OF_CONDUCT.md>
[contributing]: <https://github.com/elixir-cloud-aai/elixir-cloud-aai/blob/dev/CONTRIBUTING.md>
[checks-workflow]: .github/workflows/checks.yaml
[pre-commit-config]: .pre-commit-config.yaml
[pyproject]: pyproject.toml
[readthedocs-config]: .readthedocs.yaml
[release-workflow]: .github/workflows/release.yaml
[semver]: <https://semver.org/>
[setuptools-scm]: <https://setuptools-scm.readthedocs.io/>
[schema-service]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L158>
[schema-endpoints]: <https://github.com/ga4gh-discovery/ga4gh-service-registry/blob/8c45be52940db92c2fa1cd821519c271c22b1c4c/service-registry.yaml#L16>
[url-swagger]: <http://localhost:8080/ga4gh/registry/v1/ui>
[foca-access-control]: https://foca.readthedocs.io/en/latest/access-control/
[foca-auth]: https://foca.readthedocs.io/en/latest/authentication/
[foca-deployment]: https://foca.readthedocs.io/en/latest/deployment/
[ls-login]: https://lifescience-ri.eu/ls-login/
[protes]: https://github.com/elixir-cloud-aai/proTES
