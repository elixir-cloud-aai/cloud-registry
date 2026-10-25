# Configuration

The app is configured by
[`cloud_registry/config.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml),
the base config, which documents its values. To override some of them, write
them to a config overlay file and list it in environment variable
`FOCA_CONFIG_OVERLAY` (several files separated by `:`); FOCA merges overlays
into the config on startup (see the [FOCA deployment
guide](https://foca.readthedocs.io/en/latest/deployment/)). The [Helm
chart](deployment.md) uses the same mechanism: it renders its `config` value
(in a values file per deployment) into an overlay. Overlays cannot change the
address the image listens on: `server.host` and `server.port` apply to the
development server only, not to Gunicorn (see [Server](#server)).

## Deployment configuration

The registry works with any OpenID Connect provider that issues access tokens
as per [RFC 9068](https://www.rfc-editor.org/rfc/rfc9068) (JWTs). The
identity provider, its claims, the groups mapped to roles and the roles of
users on services are deployment configuration: the base config has no
defaults for them, so each deployment sets them in an overlay (or in the
chart's `config` value), otherwise the app refuses to start:

- `security.auth.issuers`: the trusted token issuers (the `iss` claim of their
  tokens, e.g., `https://login.example.org/oidc/`); tokens of other issuers
  are rejected (see [Authentication](access-control.md#authentication)).
  Usually one: roles are granted to users by their identifier (`sub`) and to
  groups by their name, without the issuer, so only trust several issuers if
  their identifiers and group names can't collide (FOCA warns on startup;
  see FOCA's [access
  control](https://foca.readthedocs.io/en/latest/access-control/#identity-and-group-claims));
- `security.auth.audience`: the client IDs of the apps whose tokens are
  accepted (the `aud` claim, e.g., of the registry's front end), unless
  `security.auth.verify_audience` is `false`, which accepts tokens for any
  audience (not recommended);
- `security.access_control.claim_groups`: the claim with the groups of users
  (e.g., `groups`), needed for admins and roles of groups; add it to
  `security.auth.userinfo_claims` if the identity provider releases it via
  user info only, not in access tokens (as `name`, which the registry shows
  to owners of services);
- `security.access_control.group_mapping`: the group(s) whose members are
  admins (values of the group claim, see [Access
  control](access-control.md#admins)), or
  `custom.access_control.require_admin: false` to run without admins.

For example:

```yaml
security:
    auth:
        issuers:
            - https://login.example.org/oidc/
        audience:
            - registry-portal
        userinfo_claims:
            - groups
            - name
    access_control:
        claim_groups: groups
        group_mapping:
            registry-admins: admin
```

Roles of users and groups on services are granted by owners and admins via
the API, not configured (see [Roles on
services](access-control.md#roles-on-services)). The policies
(`security.access_control.policies`, see FOCA's [access
control](https://foca.readthedocs.io/en/latest/access-control/#configuring-policies))
say what each role may do. They are named, so that an overlay can change,
add or remove single rules by name (FOCA's [named
entries](https://foca.readthedocs.io/en/latest/configuration/#named-entries)),
e.g., remove the rule that lets any authenticated user register services, so
that only admins can:

```yaml
security:
    access_control:
        policies:
            register-services: null
```

An overlay that gives the policies as a list replaces all rules of the base
config instead; FOCA refuses to start if an overlay gives named rules after
one that gives a list. Don't rename the roles `owner` and `maintainer`
(`security.access_control.resource_roles`), as the API is written for them.

The API specification (`api.specs`) is named `registry`, too, so that an
overlay can change it by name, e.g., to [require login for
reads](access-control.md#requiring-login-for-reads).

FOCA's service clients (`security.auth.service_clients`,
`security.auth.on_behalf_of_header`) are not supported; the app refuses to
start with them (see [Access control](access-control.md#roles-on-services)).

## Local overrides

Docker Compose mounts
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml),
an overlay for local development that trusts a fictitious token issuer
(`https://login.example.org/oidc/`; set your own in a local override), sets an
example group claim (`groups`), accepts tokens for any audience and `http`
service URLs, and requires no admins. To override more values locally, put
them in `config.local.yaml` and mount it in a `docker-compose.override.yaml`
(both are ignored by Git):

```yaml
services:
    cloud-registry:
        environment:
            FOCA_CONFIG_OVERLAY: /etc/cloud-registry/config.dev.yaml:/etc/cloud-registry/config.local.yaml
        volumes:
            - ./config.local.yaml:/etc/cloud-registry/config.local.yaml:ro
```

## Public URLs

Public URLs, e.g., in `Location` headers, are built from
`custom.endpoints.service` (scheme, host and port). The Helm chart sets them
from its `hostName` value; other deployments must set them, as the defaults
(`https://0.0.0.0:8080`) aren't reachable (the Docker Compose setup sets
`http://localhost:8080` in
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml)).
Set `api_path` to the path prefix in front of the API base path when the app
is served behind a reverse proxy under a subpath (e.g., `registry`), or leave
it empty. Previously, `api_path` held the API base path itself; see
[Upgrading](upgrading.md#public-urls).

## Service URL schemes

Clients send requests to the registered services, often with access tokens,
so service URLs (`url`) must use one of the schemes listed in
`custom.endpoints.services.url_schemes`: `https` only by default; `http` must
be allowed explicitly (e.g., `[http, https]`). Registrations (`POST
/services`, `PUT /services/{serviceId}`) of services with other schemes are
rejected with `400 Bad Request`, naming the allowed schemes. Only `http` and
`https` can be listed, and the list must not be empty. Links
(`documentationUrl`, `organization.url`) can be HTTP(S) URLs either way, and
`contactUrl` also a `mailto:` URL (see [Registrations](api.md#registrations)).
The Docker Compose setup allows `http` too, in
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml),
e.g., for services running locally; the Helm chart keeps the default (set
`config.custom.endpoints.services.url_schemes` to change it).

On startup, the app warns of stored services whose URLs use other schemes
(e.g., `http` URLs registered while `http` was allowed, or before this
check): they are still served, but can only be updated with an allowed
scheme (see [Upgrading](upgrading.md#stored-services)).

## Service environments

The GA4GH specification describes the environment of a service
(`environment`) as a free string, and suggests `prod`, `test`, `dev` and
`staging`. So that filters by environment work (e.g., without `Prod` or
`production` besides `prod`), registrations may only use the values listed
in `custom.endpoints.services.environments`: these four by default.
Registrations (`POST /services`, `PUT /services/{serviceId}`) with other
values are rejected with `400 Bad Request`, naming the allowed values
(case-sensitive); services without `environment` are accepted. An empty
list allows any value. For example, in a config overlay (with the Helm
chart, in `config`):

```yaml
custom:
    endpoints:
        services:
            environments:
                - production
                - testing
```

Clients that let users choose an environment (e.g., UIs) must offer the
same values. Stored services with other values are served as they are, and
only checked when they are next written (see
[Upgrading](upgrading.md#stored-services)).

## Request size limit

Request bodies are limited to `server.max_content_length` bytes (default:
1048576, i.e., 1 MiB), by
[FOCA](https://foca.readthedocs.io/en/latest/configuration/server/). Larger
requests are rejected with `413 Content Too Large` before the access token is
checked and the body is parsed: requests whose `Content-Length` exceeds the
limit are rejected without reading the body (and requests with an invalid
`Content-Length` with `400`). Bodies of unknown size (`Transfer-Encoding:
chunked`) are read up to one byte over the limit, and rejected if they are
larger. The app refuses to start with `null` (which would remove the limit in
FOCA).

Set the same limit at the ingress or reverse proxy in front of the app, so
that it rejects larger requests itself; the Helm chart sets none, as it
depends on the proxy (e.g., ingress-nginx limits request bodies to 1 MiB by
default, the app's default; see [Ingress](deployment.md#ingress)).

## Server

The image serves the app with [Gunicorn](https://gunicorn.org/) on port 8080,
with settings for a small container in
[`cloud_registry/gunicorn_config.py`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/gunicorn_config.py):
one worker process with 4 threads, workers restarted if their main loop
doesn't respond for 30 seconds (requests run in threads, so slow requests
don't count), 20 seconds to finish requests on shutdown, idle keep-alive
connections closed after 100 seconds, and Gunicorn's size limits of the request
line and headers. Keep the keep-alive timeout longer than that of a reverse
proxy in front of the app for its connections to the app (e.g., 60 seconds
for ingress-nginx, 90 seconds for Traefik): otherwise, the proxy may reuse a
connection just as Gunicorn closes it, and the request fails (`502`). To change the settings, set
`GUNICORN_CMD_ARGS` to [Gunicorn
options](https://gunicorn.org/reference/settings/) (e.g., `--workers 2
--threads 8`); Docker Compose passes it on from the host, and the Helm chart
sets it from its `cloudRegistry.gunicorn` values.

Each worker loads the whole app (about 55 MiB of memory), with its own
database client, and runs the startup checks (e.g., of the service info and
the admin groups); if they fail, Gunicorn exits. Gunicorn always listens on
`0.0.0.0:8080` (its `bind` setting): `server.host` and `server.port` of the
config apply only to Flask's development server
(`python -m cloud_registry.app`), which is not for production, and changing
them does not move the image's traffic.

Serve the app behind a reverse proxy that buffers requests (e.g.,
ingress-nginx, or Traefik with its `buffering` middleware): Gunicorn's
threads read requests themselves, so slow clients can tie them up.

## Service info

`GET /service-info` serves the service info from the config
(`service_info.service`), without the database; it is served by
[FOCA](https://foca.readthedocs.io/en/latest/configuration/service-info/). On
startup, the service info is checked against the GA4GH `Service` schema, and
the app refuses to start if it is invalid, naming the invalid field. `version`
defaults to the version of the installed app (`service_info.package`);
`createdAt` and `updatedAt` are served only if set, and must be RFC 3339
date-times (e.g., `2020-11-08T00:00:00Z`). The defaults describe the app, not a
deployment: `id` (`org.example.cloud-registry`) and `organization` (`Example
Organization`, `https://example.org`) are placeholders with reserved example
domains, and there is no `contactUrl`. Deployments set their own `id` (unique,
e.g., in reverse domain name notation), `name`, `description`, `organization`,
`contactUrl` and `environment` (the default is `dev`); while `id` or the name
or URL of `organization` are the placeholders, the app logs a warning on
startup, naming the fields to set (also with Docker Compose, whose
development config keeps them). Fields and list items set to `null`
(e.g., in a config overlay) are not served. To change the service info, change
the config and restart the app (the Helm chart restarts it when the `config`
value changes). Previously, service info was set via the API; see
[Upgrading](upgrading.md#service-info).
