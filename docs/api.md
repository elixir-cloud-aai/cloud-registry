# API

The endpoints of the [GA4GH Service Registry
API](https://github.com/ga4gh-discovery/ga4gh-service-registry) (version
1.0.0) are unchanged: the registry does not extend their requests or
responses, except for an extension field of the service info (see
[Registration rules](#registration-rules)), as the GA4GH specifications
extend it. Everything else is added as extra operations and endpoints, all
under the API base path `/ga4gh/registry/v1`:

| Endpoint | Methods | Who |
| --- | --- | --- |
| `/service-info` | `GET` (GA4GH) | anyone |
| `/services` | `GET` (GA4GH) | anyone |
| `/services` | `POST` | any authenticated user |
| `/services/types` | `GET` (GA4GH) | anyone |
| `/services/types` | `POST` | any authenticated user (mode `controlled`: admins) |
| `/services/types` | `DELETE` | admins |
| `/services/types/approval` | `PUT` | admins |
| `/services/{serviceId}` | `GET` (GA4GH) | anyone |
| `/services/{serviceId}` | `PUT` | owners, maintainers, admins (new identifiers: admins) |
| `/services/{serviceId}` | `DELETE` | owners, admins |
| `/services/{serviceId}/rename` | `POST` | admins |
| `/services/{serviceId}/permissions` | `GET` | owners, admins |
| `/services/{serviceId}/permissions/{userId}` | `PUT`, `DELETE` | owners, admins |
| `/services/{serviceId}/permissions/groups/{group}` | `PUT`, `DELETE` | owners, admins (`PUT` by owners: groups they are members of) |
| `/services/{serviceId}/verification` | `GET` | anyone |
| `/services/{serviceId}/verification` | `PUT`, `DELETE` | admins |
| `/verifications` | `GET` | anyone |
| `/approvals` | `GET` | any authenticated user |
| `/me` | `GET` | any authenticated user |

Admins have their rights only in [admin mode](access-control.md#admin-mode):
requests with the header `X-Request-Roles: admin`. Otherwise, they have the
rights of other users.

Deployments can [require login](access-control.md#requiring-login-for-reads)
for the reads by anyone, except `GET /service-info`. Otherwise, these reads
accept requests without and with an access token: with one, they show what
the user may see (e.g., [pending](#pending-approvals) services of the user).
Invalid or expired tokens are rejected (`401 Unauthorized`, with
`WWW-Authenticate: Bearer error="invalid_token"`, as on other operations),
not treated as anonymous, so that clients notice and log in again. Tokens
that can't be validated for other reasons (e.g., the identity provider is
unavailable) are: the reads then keep working, without what only the user
may see.

The [Swagger UI](getting-started.md#quick-start) of a deployment documents
all operations and serves the API specification
(`/ga4gh/registry/v1/openapi.json`).

## Deviations from the GA4GH specification

The GA4GH endpoints keep their requests and responses, but some of their
semantics differ, so that clients can rely on the registry's data:

- `GET /services/types` lists the stored types, also types that no service
  has (yet), rather than the types of the registered services (see [Service
  types](#service-types)).
- `createdAt` and `updatedAt` describe the registry's entry, and the
  registry sets them, rather than describing the deployment of the service
  (see [Timestamps](#timestamps)).
- `environment` must be one of the values that the deployment allows, by
  default `prod`, `test`, `dev` and `staging`, which the specification only
  suggests (see [Service
  environments](configuration.md#service-environments)).
- In mode `moderated`, the reads don't list pending services and types to
  other users (see [Pending approvals](#pending-approvals)).
- `GET /service-info` serves the registration rules of the deployment
  (`registration_rules`) besides the fields of the GA4GH `Service` (see
  [Registration rules](#registration-rules)).

## Registration rules

Registrations must follow rules that each deployment configures. So that
clients (e.g., UIs, the [ELIXIR Cloud
Components](https://github.com/elixir-cloud-aai/cloud-components)) can check
registrations as the registry does, e.g., offer only the allowed values in
forms, `GET /service-info` serves them in `registration_rules`:

```json
{
  "id": "org.example.cloud-registry",
  "name": "Cloud Registry",
  "...": "...",
  "registration_rules": {
    "environments": ["prod", "test", "dev", "staging"],
    "url_schemes": ["https"],
    "service_types": "open",
    "max_unused_service_types": 10
  }
}
```

- `environments`: the values allowed for the environment of services
  (`environment`, case-sensitive); empty if any value is allowed (see
  [Service environments](configuration.md#service-environments));
- `url_schemes`: the schemes allowed in service URLs (`url`): `https`, and
  `http` if the deployment allows it (see [Service URL
  schemes](configuration.md#service-url-schemes));
- `service_types`: who may add service types, `open`, `controlled` or
  `moderated` (see [Service types](#service-types));
- `max_unused_service_types`: how many types that no service has users other
  than admins may have added (`409 Conflict` for further ones); `0` if they
  can't add types, and not set if there is no limit; concurrent requests of
  a user can exceed it by their number (see [Service
  types](configuration.md#service-types)).

The registry builds the rules from its config on each request, so they are
always those it enforces; the rules for the current user (e.g., whether they
may add types, as admins always may) are in `GET /me`. Read the rules
rather than configuring them in clients, which would go out of date when the
deployment changes them. Like all of the service info, they are public, also
if the deployment [requires login](access-control.md#requiring-login-for-reads)
for other reads. The OpenAPI specification describes them as an extension of
the GA4GH `Service` schema (`RegistryServiceInfo`), as GA4GH APIs extend it
for their service info (e.g., `TesServiceInfo` of the TES specification);
their fields are `snake_case`, like the other fields the registry adds.

## Registrations

Registering a service (`POST /services`, or `PUT /services/{serviceId}` with a
new identifier) returns `201 Created`, with the identifier of the service in
the body and its [public URL](configuration.md#public-urls) in the `Location`
header; updating a service (`PUT`) returns `200 OK`. Only admins can register
services with `PUT`: others can only update services they have a role on
(`403 Forbidden` otherwise). Services deleted while they are registered or
updated return `404 Not Found`, unless an admin's `PUT` registers them again.
Identifiers `.` and `..` (which clients resolve in URLs) and `types` (the path
of `GET /services/types`) are rejected (`400 Bad Request`), as are users and
groups with such segments in the paths of the [permissions
endpoints](access-control.md#roles-on-services).

URLs in registrations must be HTTP(S) URLs with a host: the service `url`,
`documentationUrl`, `organization.url` and `contactUrl`, which can also be an
email address (`mailto:`); other URLs are rejected (`400 Bad Request`, saying
why). Service URLs must be `https` URLs by default, as clients send access
tokens to the services; deployments can allow `http` (see [Service URL
schemes](configuration.md#service-url-schemes)). Links are stored as written,
so they must not contain whitespace (unlike service URLs, which may have
leading and trailing spaces), control or format characters (except joiners in
international host names), and no URLs may contain malformed
percent-encodings, or ASCII characters that URLs only allow percent-encoded
(e.g., `<`, `|`, `\`). Hosts must be domain or registered names (without
percent-encodings or empty labels; international names valid as per IDNA
2008), IPv4 addresses in dotted-decimal form (not, e.g., `127.1`) or IPv6
addresses in brackets. URLs are checked and normalized with FOCA's [URL
helpers](https://foca.readthedocs.io/en/latest/utilities/#checking-and-normalizing-urls).
Anyone logged in can register services, so
clients should treat registry content as untrusted: escape it rather than
render it as HTML, and don't follow or fetch its URLs blindly (e.g., to
internal addresses).

The environment of a service (`environment`) must be one of the values
that the deployment allows: by default, those that the GA4GH specification
suggests (`prod`, `test`, `dev`, `staging`); others are rejected (`400 Bad
Request`; see [Service environments](configuration.md#service-environments)).

Request bodies are limited to 1 MiB by default (`413 Content Too Large`; see
[Request size limit](configuration.md#request-size-limit)). Strings in the
bodies of registrations (`POST /services`, `PUT /services/{serviceId}`) and
verifications (`PUT /services/{serviceId}/verification`) are limited in length
(longer ones are rejected with `400 Bad Request`):

| Fields | Maximum length (characters) |
| --- | --- |
| URLs: `url`, `organization.url`, `contactUrl`, `documentationUrl` | 2048 |
| Names: `name`, `organization.name` | 1024 |
| `description` | 4096 |
| `version`, `environment`, `type.group`, `type.artifact`, `type.version` | 256 |

Services stored before the checks of lengths were introduced may not pass
them; see [Upgrading](upgrading.md#stored-services).

The number of registrations per user is not limited, so deployments should
limit request rates at the ingress (see [Rate
limits](deployment.md#rate-limits)). Users other than admins may only have
added a limited number of service types that no service has (`409 Conflict`
for further ones; see [Service types](configuration.md#service-types)).

## Renaming services

Admins can change the identifier of a service (`POST
/services/{serviceId}/rename`, with the new identifier in the body, e.g.,
`{"id": "my-tes"}`; `200 OK`, with the new identifier). Everything stored
under the old identifier moves to the new one: the service, with its
[verification](#verified-services) and `createdAt` (`updatedAt` is renewed),
the roles of users and groups on it and the stored names of these users.
Registering the service again under a new identifier and deleting the old one
would lose all of these.

The service is never registered under both identifiers: it moves in a single
write, after which the old identifier is not found (`404 Not Found`) and
links to it break. Owners and maintainers keep their roles, which move right
after it; clients that stored the old identifier must use the new one.
New identifiers that are registered (also the service's own) are rejected
(`409 Conflict`, code `service-id-taken`); so are identifiers that can't be
used in URLs (as for `PUT`, see [Registrations](#registrations); also
slashes) or in roles (commas, line breaks), with `400 Bad Request`.

Granting and revoking roles via the registry's permission endpoints
(`/services/{serviceId}/permissions/...`) waits for renames, but via FOCA's
permissions API (for admins: `/admin/access-control/permissions`) doesn't:
change roles on services via the registry's endpoints, or not while the
service is renamed (roles changed meanwhile may be lost, or left on the old
identifier). Lists of roles read during a rename may be incomplete.

Renames don't need MongoDB transactions (which need a replica set): while a
service is renamed, both identifiers are locked (collection `service_locks`),
so that the rename doesn't interleave with other changes of either service
(`PUT`, `DELETE`, granting and revoking roles) or other renames. Renames and
changes wait for each other for up to about a second, then give up (`409
Conflict`, code `service-busy`; try again). If a rename fails after the
service moved (e.g., the app crashes), the service may lack roles, and both
identifiers stay locked, until the rename is finished, a minute after it
started: when the app next handles a request of the registry's operations
(not of the Swagger UI, the spec, the service info or FOCA's admin API), or
starts. Renames are logged with the admin.

## Timestamps

The registry sets the timestamps of services, so that clients can rely on
them (e.g., to sort services or show when they were last changed):

- `createdAt`: when the service was registered; kept on updates;
- `updatedAt`: when the service was registered or last updated (each `PUT`,
  and [renames](#renaming-services)).

The registry sets them as RFC 3339 date-times in UTC, in milliseconds
(e.g., `2026-10-20T12:34:56.789Z`). Timestamps sent in registrations are
ignored (not an error, so clients can send services back as they got them;
like all fields, they must be strings), except for admins: they can set
them, e.g., to keep the timestamps of services imported from another
registry. Those are stored as sent, so they may have other time zones or
precisions. `updatedAt` is only kept for services that admins register
(e.g., import); updates always set it. Timestamps that admins don't send
are set as for other users. Timestamps of admins must be RFC 3339
date-times (e.g., `2019-06-04T12:58:19Z`); others are rejected (`400 Bad
Request`), so admins who update a service with another stored `createdAt`
send a valid one, or none.
Verifying a service doesn't change its timestamps.

This differs from the GA4GH specification, where the timestamps describe
the deployment of the service (when it was first deployed, and last
updated): in the registry, they describe its entry. Services registered
before the registry set the timestamps keep those they were registered
with, and services registered without a valid `createdAt` (an RFC 3339
date-time) don't get one (see
[Upgrading](upgrading.md#stored-services)).

## Service types

Service types (`type` of services: `group`, `artifact` and `version`, e.g.,
`org.ga4gh`, `tes`, `1.1.0`) are registered on their own, so that clients
can offer them to choose from. `GET /services/types` lists them, sorted by
`group`, `artifact` and `version`; this differs from the GA4GH
specification, where it lists the types of the registered services: types
can be registered before any service has them. Every type of a registered
service is registered, too.

```bash
curl -X POST "http://localhost:8080/ga4gh/registry/v1/services/types" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}'
```

`POST /services/types` registers a type and returns it (`201 Created`);
types are unique as written (`409 Conflict` if it is registered).
Registering or updating a service with a new type registers the type, too.
The deployment configures who may add types (see [Service
types](configuration.md#service-types)):

- `open` (the default): any authenticated user;
- `controlled`: admins only. Other users get `403 Forbidden` for `POST
  /services/types`, and registering or updating a service with a type that
  isn't registered returns `400 Bad Request`, naming the type. Admins'
  services may have new types, which are registered.
- `moderated`: any authenticated user, but the types of users other than
  admins are pending until an admin approves them (see [Pending
  approvals](#pending-approvals)); admins' types are approved at once.

`GET /me` tells clients whether the current user may add types
(`may_add_service_types`), e.g., to offer "Add new service type" only then,
and whether their new types are pending (`service_types_pending`); `GET
/service-info` tells them the mode and the limit of unused types (see
[Registration rules](#registration-rules)).

In every mode, only admins can remove types: `DELETE
/services/types?group=<group>&artifact=<artifact>&version=<version>`
returns the removed type, and `404 Not Found` for types that aren't
registered. Users who add types don't own them, as other users' services
may have them. Types that registered services have can't be removed (`409
Conflict`: "Service type is in use. If you really want to revoke it, edit or
delete all services that depend on it."). The registry stores who added each
type (not served).

On startup, the app registers the types of the registered services that
aren't registered (e.g., when upgrading from a version that didn't store
types; see [Upgrading](upgrading.md#service-types)).

### Pending approvals

In mode `moderated`, the types that users other than admins add (explicitly
or with a service of a new type) are pending until an admin approves them,
and so are the services that have a pending type, also those of other
users. Pending types and services are only visible to admins and to the
users who added them (for services: users with a role on them):

- `GET /services`, `GET /services/{serviceId}` (`404 Not Found`) and `GET
  /services/types` don't list them to others, nor do the reads of
  [verifications](#verified-services). Requests without a token have no
  user, so these reads list pending types and services to nobody then;
  send a token to see them.
- `GET /approvals` (login required) lists the pending types
  (`service_types`) and services (`services`) the user may see: all for
  admins.
- Only admins can update pending services (`409 Conflict` for others, so
  that a service doesn't change while an admin reviews it), other users only
  to an approved type (e.g., to undo a change of the type); their owners can
  delete them.

Admins approve a type with `PUT /services/types/approval` and the type in
the body; it and the services that have it are then listed like all others.
There is no way back: admins remove types instead (only types that no
service has, see above). Pending types stay pending if the mode changes.
An admin who registers a service with a pending type doesn't approve it:
admins approve types with `PUT /services/types/approval`. A pending type is
stored only for a valid service, before the service is stored, so it stays
pending if the registration then fails (e.g., for a duplicate registered
meanwhile), and also if the service is then changed back to its previous
type: pending types stay when no service has them. Admins remove such types,
as other unwanted ones. Types are unique also while pending: `POST
/services/types` returns `409 Conflict` for a type that another user added.

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/types/approval" \
  -H "Authorization: Bearer <admin token>" -H "X-Request-Roles: admin" \
  -H "Content-Type: application/json" \
  -d '{"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}'
```

## Errors

Requests that don't match the API specification (e.g., missing or unknown
fields, values that are too long, extra query parameters, or bodies that
aren't JSON) are rejected with `400 Bad Request`, with the reason in `detail`.

Errors handled by the app are [problem
details](https://www.rfc-editor.org/rfc/rfc7807) (`title`, `status` and
usually `detail`, e.g., `{"title": "Not found", "status": 404, "detail": "The
requested resource wasn't found."}`), sent as `application/json`, as the
GA4GH specification declares (FOCA's `exceptions.media_type`), including
those of authentication and access control, unknown paths and FOCA's
permissions API. They keep the headers of the HTTP errors, e.g., `Allow`
for `405 Method Not Allowed` and the Bearer challenge (`WWW-Authenticate:
Bearer`, RFC 6750) for `401 Unauthorized` (see
[Authentication](access-control.md#authentication)). Gunicorn rejects
malformed requests itself, before they reach the app (e.g., with an invalid
`Content-Length`, or a request line or headers over its limits), with HTML
error pages.

Conflicts (`409 Conflict`) also have a `code`, which tells them apart:
unlike `detail`, codes are stable, so clients branch on them (e.g., `{"title":
"Conflict", "status": 409, "detail": "...", "code": "pending-service"}`).

| Code | Conflict |
|---|---|
| `duplicate-service` | a service with the same URL and API type is registered (see [Duplicate services](#duplicate-services)) |
| `pending-service` | the service is [pending](#pending-approvals): only admins update it, and it can't be verified |
| `unused-types-limit` | the user has added as many service types that no service has as allowed (see [Service types](configuration.md#service-types)) |
| `service-type-registered` | the service type is registered, also if it is pending |
| `service-type-in-use` | services have the service type, so it can't be removed |
| `verification-mismatch` | the service has another URL or API type than the one checked (see [Verified services](#verified-services)) |
| `last-owner` | the subject is the last owner of the service |
| `service-id-taken` | a service with the new identifier is registered (see [Renaming services](#renaming-services)) |
| `service-busy` | the service, or the new identifier, is being renamed or changed (see [Renaming services](#renaming-services)) |
| `conflict` | other conflicts (e.g., changes of policy rules set in the configuration via FOCA's permissions API) |

Further codes may be added; treat unknown ones like `conflict`. The API
specification documents the codes in the schema `ConflictError`.

See [Access control](access-control.md) for the roles and [Verified
services](#verified-services) for verifications. Extensions of the GA4GH
endpoints, such as pagination, search or filters, will be reconsidered with
the upcoming new version of the Service Registry specification.

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
identifier of the registered service in `serviceId` (and in `detail`), so
that users can contact its owner or an admin. Only users who may read the
registered service (`GET /services/{serviceId}`) get its identifier; if a
deployment [restricts reads](access-control.md#requiring-login-for-reads),
other users get a `409` without it.

As anyone logged in can register a URL first, including one of a deployment
they don't operate, a squatter could block the real operator of a service.
Admins resolve such cases: they can delete the entry, or grant the real
operator the role `owner` on it and revoke the squatter's role. Consumers that
need trustworthy entries should rely on entries checked by admins (see
[Verified services](#verified-services)). Proving control of a
URL (e.g., by fetching `{url}/service-info` on registration) was rejected:
the identifier in service info is self-declared, and fetching user-supplied
URLs would allow server-side request forgery.

Services stored before this check are checked on startup (see
[Upgrading](upgrading.md#stored-services)).

## Verified services

Admins mark services as **verified** once they have checked them, e.g., that
a service accepts tokens of the deployment's identity provider. What admins
check is up to the
deployment; the registry only records when a service was verified. Owners
can't verify their own services.

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/verification" \
  -H "Authorization: Bearer <admin token>" -H "X-Request-Roles: admin" \
  -H "Content-Type: application/json" \
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

Verifications can be read by anyone (unless the deployment [requires
login](access-control.md#requiring-login-for-reads)):
`GET /services/<id>/verification`
returns the verification of a service (`404 Not Found` if it is not
verified), and `GET /verifications` lists all verified services, sorted by
identifier, each with its identifier as `service_id` and the fields above.

The GA4GH endpoints are unchanged: `GET /services` and
`GET /services/<id>` don't show verifications. Clients that should only use
verified services (e.g., a [proTES](https://github.com/elixir-cloud-aai/proTES)
gateway choosing TES instances) list the services (`GET /services`) and keep
those listed by `GET /verifications` with the same identifier, `url` and
`type` (`group` and `artifact`), so that a service changed between the two
requests isn't used.

A verification applies to the service as it was when it was verified:

- **URL, as written:** updating the service with any other URL removes the
  verification, even another spelling of the same one (e.g., another case
  of the host, a trailing slash, or a query or fragment, which are ignored
  when [comparing services](#duplicate-services)).
- **API type:** updating `type.group` or `type.artifact` (compared as for
  duplicates) removes it.
- **Pending:** pending services (see [Pending
  approvals](#pending-approvals)) can't be verified (`409 Conflict`; approve
  their type first), and services that become pending (e.g., updated to a
  new, pending `type.version`) lose their verification.
- Changing them back doesn't restore it. Other updates (e.g., of `version`,
  `type.version` or `name`) keep it.
- Deleting a service deletes its verification.
- Admins should send the URL and API type they checked when verifying (see
  above), so that a service changed meanwhile isn't verified.
