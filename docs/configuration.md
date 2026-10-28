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
  Usually one; for several, see [Several token
  issuers](#several-token-issuers);
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
  `security.access_control.require_admin: false` to run without admins.

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

## Several token issuers

Deployments can trust several token issuers, e.g., an institutional login
besides a community one. Their users are then told apart by issuer: FOCA
namespaces the identifiers of users (`sub`) by issuer, as
`<namespace>|<sub>` (by default, the namespace is the issuer, e.g.,
`https://login.example.org/oidc/|abc123`), so equal identifiers of
different issuers are different users, also as owners and maintainers of
services and as the users who added service types. `GET /me` returns the
namespaced identifier (`user_id`), which owners use to grant roles (see
[Roles on services](access-control.md#roles-on-services)).

Issuers can be listed with settings of their own (FOCA's `IssuerConfig`),
which replace the global ones for their tokens: `namespace`, `audience`,
`claim_groups` and `userinfo_claims`, e.g.:

```yaml
security:
    auth:
        issuers:
            # Trusted so far: keeps its users' identifiers bare, so that
            # their stored roles stay valid
            - issuer: https://login.example.org/oidc/
              namespace: ""
            # Added: its users are namespaced (`https://login.other.org/|<sub>`)
            - issuer: https://login.other.org/
              audience:
                  - registry-portal-other
              # Its groups don't count (e.g., for admins), as they could
              # collide with those of the first issuer
              claim_groups: ""
        audience:
            - registry-portal
```

Namespaces must not start with `groups/`, the path of the roles of
groups in the permissions API (the app refuses to start).

Group claim values are not namespaced: groups of different issuers with
the same name are the same group, also for the admin groups
(`security.access_control.group_mapping`) and [roles of
groups](access-control.md#roles-of-groups). Only take groups from issuers
that can't issue each other's groups, e.g., by setting `claim_groups: ""`
for the others. To add an issuer to a running deployment, see
[Upgrading](upgrading.md#several-token-issuers).

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

Public URLs, e.g., in `Location` headers, are built from the public URL of
the app as clients see it, FOCA's
[`server.public_url`](https://foca.readthedocs.io/en/latest/configuration/server/#public-url),
including any path prefix of a reverse proxy (e.g.,
`https://registry.example.org/` or `https://example.org/registry/`). The app
refuses to start without it. The Helm chart sets it to
`https://<hostName>/`, or to its `publicUrl` value; other deployments must
set it (the Docker Compose setup sets `http://localhost:8080` in
[`config.dev.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/config.dev.yaml)).
It replaces `custom.endpoints.service` of earlier versions; see
[Upgrading](upgrading.md#from-050).

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
Clients read the allowed schemes from the service info (see [Registration
rules](api.md#registration-rules)). The Docker Compose setup allows `http`
too, in
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
same values: they read them from the service info rather than configuring
them (see [Registration rules](api.md#registration-rules)). Stored services
with other values are served as they are, and only checked when they are
next written; the app logs a warning on startup that names them (see
[Upgrading](upgrading.md#stored-services)).

## Service types

`custom.access_control.service_types` sets who may add [service
types](api.md#service-types):

- `open` (the default): any authenticated user, explicitly (`POST
  /services/types`) or by registering a service with a new type;
- `controlled`: admins only; other users can only register services with
  registered types, so that the registry offers a curated list of types;
- `moderated`: any authenticated user, but the types of users other than
  admins, and the services that have them, are pending until an admin
  approves them (see [Pending approvals](api.md#pending-approvals)), so that
  admins curate the list without being asked for each type.
  Switch to this mode only once all instances of the app support it (e.g.,
  after a rolling update): older versions (0.4.0) store no types, so the
  services they register are served at once, and their types are added as
  approved when the app next starts (with the types of all stored services).

Users other than admins may only have added
`custom.access_control.max_unused_service_types` types that no service has
(default: 10; `null` for no limit), in every mode: further types are rejected
(`409`), whether added explicitly or by registering or changing services,
until services have them or admins remove them (approved types count too).
Types become unused when their services are deleted or change their type, so
the limit bounds how many types each user can add without services (users
can still leave more types unused by registering services with new types and
deleting them, one registration per type); set `0`
to let only admins add types other than those of services. Concurrent
requests of a user can exceed the limit by their number. Clients read the
mode and the limit from the service info, rather than configuring them (see
[Registration rules](api.md#registration-rules)).

The limit is per user, so accounts that anyone can create (e.g., with LS
Login) can still add types, and pending types in mode `moderated`, without
bound. Reads don't slow down with the number of pending types, as services
are marked as pending when stored, but `GET /services/types` and `GET
/approvals` list them all. Rate-limit write requests, at least `POST
/services/types` and `POST /services`, at the ingress or reverse proxy in
front of the app (see [Rate limits](deployment.md#rate-limits)), and remove
unwanted types as an admin (`DELETE /services/types`).

Only admins can remove types, in every mode. Set this option rather than
removing the policy `add-service-types` (in `security.access_control.policies`),
which only applies to `POST /services/types`: users also add types by
registering services, and `GET /me` (`may_add_service_types`) only reflects
this option. Overlays that give the policies as a list must include that
rule, and `list-approvals` for `GET /approvals`. For example, in a config
overlay (with the Helm chart, in `config`):

```yaml
custom:
    access_control:
        service_types: controlled
        max_unused_service_types: 5
```

## Invitations

`custom.access_control.invitations` sets how long [invitations to
services](access-control.md#invitations) are valid (`expiry_days`, default:
7) and how many that haven't expired a service may have (`max_per_service`,
default: 20; `null` for no limit). For example, in a config overlay (with the
Helm chart, in `config`):

```yaml
custom:
    access_control:
        invitations:
            expiry_days: 2
            max_per_service: 5
```

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
with FOCA's settings for apps in containers (`foca.gunicorn_config`; see
[Running in
production](https://foca.readthedocs.io/en/latest/deployment/#running-in-production)
in FOCA's docs): one worker process with 4 threads, and idle keep-alive
connections closed after 100 seconds. Keep the keep-alive timeout longer than
that of a reverse proxy in front of the app for its connections to the app
(e.g., 60 seconds for ingress-nginx, 90 seconds for Traefik): otherwise, the
proxy may reuse a connection just as Gunicorn closes it, and the request
fails (`502`). To change the settings, set `GUNICORN_CMD_ARGS` to [Gunicorn
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
(e.g., in a config overlay) are not served. The app adds the [registration
rules](api.md#registration-rules) (`registration_rules`), built from the
`custom` config on each request (`service_info.runtime_fields`): keep that
option, and don't set `registration_rules` in `service_info.service` (not
even to `null`), so that they are always the rules the registry enforces;
the app refuses to start otherwise. To change the service info, change
the config and restart the app (the Helm chart restarts it when the `config`
value changes). Previously, service info was set via the API; see
[Upgrading](upgrading.md#service-info).
