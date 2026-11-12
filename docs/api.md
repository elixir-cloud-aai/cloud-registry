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
| `/service-info/listing` | `GET`, `PUT`, `DELETE` | admins |
| `/services` | `GET` (GA4GH) | anyone |
| `/services` | `POST` | any authenticated user |
| `/services/import` | `POST` | any authenticated user |
| `/services/types` | `GET` (GA4GH) | anyone |
| `/services/types` | `POST` | any authenticated user (mode `controlled`: admins) |
| `/services/types` | `DELETE` | admins |
| `/services/types/approval` | `PUT` | admins |
| `/services/{serviceId}` | `GET` (GA4GH) | anyone |
| `/services/{serviceId}` | `PUT` | owners, maintainers, admins (new identifiers: admins) |
| `/services/{serviceId}` | `DELETE` | owners, admins |
| `/services/{serviceId}/rename` | `POST` | admins |
| `/services/{serviceId}/permissions` | `GET` | owners, admins |
| `/services/{serviceId}/permissions/users/{userId}` | `PUT`, `DELETE` | owners, admins |
| `/services/{serviceId}/permissions/groups/{group}` | `PUT`, `DELETE` | owners, admins (`PUT` by owners: groups they are members of) |
| `/services/{serviceId}/invitations` | `GET`, `POST` | owners, admins |
| `/services/{serviceId}/invitations/{invitationId}` | `DELETE` | owners, admins |
| `/invitations/accept` | `POST` | any authenticated user |
| `/services/{serviceId}/verification` | `GET` | anyone |
| `/services/{serviceId}/verification` | `PUT`, `DELETE` | admins |
| `/verifications` | `GET` | anyone |
| `/services/{serviceId}/sync` | `GET` | anyone |
| `/services/{serviceId}/sync` | `PUT`, `DELETE` | owners, maintainers, admins |
| `/syncs` | `GET` | anyone |
| `/notifications` | `GET` | any authenticated user |
| `/notifications/{notificationId}` | `DELETE` | any authenticated user |
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
  (`registrationRules`) besides the fields of the GA4GH `Service` (see
  [Registration rules](#registration-rules)).

## Registration rules

Registrations must follow rules that each deployment configures. So that
clients (e.g., UIs, the [Cloud
Components](https://github.com/elixir-cloud-aai/cloud-components)) can check
registrations as the registry does, e.g., offer only the allowed values in
forms, `GET /service-info` serves them in `registrationRules`:

```json
{
  "id": "org.example.cloud-registry",
  "name": "Cloud Registry",
  "...": "...",
  "registrationRules": {
    "environments": ["prod", "test", "dev", "staging"],
    "urlSchemes": ["https"],
    "serviceTypes": "open",
    "maxUnusedServiceTypes": 10,
    "maxServicesPerUser": 50,
    "serviceInfoSync": {"enabled": true, "intervalHours": 24}
  }
}
```

- `environments`: the values allowed for the environment of services
  (`environment`, case-sensitive); empty if any value is allowed (see
  [Service environments](configuration.md#service-environments));
- `urlSchemes`: the schemes allowed in service URLs (`url`): `https`, and
  `http` if the deployment allows it (see [Service URL
  schemes](configuration.md#service-url-schemes));
- `serviceTypes`: who may add service types, `open`, `controlled` or
  `moderated` (see [Service types](#service-types));
- `maxUnusedServiceTypes`: how many types that no service has users other
  than admins may have added (`409 Conflict` for further ones); `0` if they
  can't add types (always in mode `controlled`), and not set if there is no
  limit; concurrent requests of a user can exceed it by their number (see
  [Service types](configuration.md#service-types));
- `maxServicesPerUser`: how many of the registered services users other
  than admins may have registered (`409 Conflict` for further ones); `0` if
  only admins can register services, and not set if there is no limit (see
  [Services per user](configuration.md#services-per-user));
- `serviceInfoSync`: whether services can be [synced](#synced-services)
  (`enabled`), and how often (`intervalHours`, 1 to 8760; see [Service info
  sync](configuration.md#service-info-sync)).

The registry builds the rules from its config on each request, so they are
always those it enforces; the rules for the current user (e.g., whether they
may add types, as admins always may) are in `GET /me`. Read the rules
rather than configuring them in clients, which would go out of date when the
deployment changes them. Like all of the service info, they are public, also
if the deployment [requires login](access-control.md#requiring-login-for-reads)
for other reads. The OpenAPI specification describes them as an extension of
the GA4GH `Service` schema (`RegistryServiceInfo`), as GA4GH APIs extend it
for their service info (e.g., `TesServiceInfo` of the TES specification).

## API style

Additions follow the style of the spec they extend: the fields and query
parameters that the registry adds are camelCase, as in the GA4GH Service Info
and Service Registry specifications (`createdAt`, `contactUrl`). Additions to
spec objects are kept to a minimum and grouped under one key (e.g.,
`registrationRules` of the service info). Keys of the config stay `snake_case`,
FOCA's style; where the service info publishes a setting, its field is camelCase
(`url_schemes` as `urlSchemes`).

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
why, without quoting the URL; see [Errors](#errors)). Service URLs must be
`https` URLs by default, as clients send access tokens to the services;
deployments can allow `http` (see [Service URL
schemes](configuration.md#service-url-schemes)). URLs are stored as written,
so they must not contain whitespace (also not around them), control or format
characters (except joiners in international host names), and no URLs may
contain malformed percent-encodings, or ASCII characters that URLs only allow
percent-encoded (e.g., `<`, `|`, `\`). Hosts must be domain or registered names
(without percent-encodings or empty labels; international names valid as per IDNA
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

Organizations (`organization`) may only have a `name` and a `url`; other
fields are rejected (`400 Bad Request`), so that their size is limited too.

Users other than admins may only have registered a limited number of the
registered services (50 by default; `409 Conflict` with code `services-limit`
for further ones; see [Services per user](configuration.md#services-per-user)),
and added a limited number of service types that no service has (`409
Conflict` for further ones; see [Service
types](configuration.md#service-types)). Requests per user and time are not
limited, so deployments should limit request rates at the ingress (see [Rate
limits](deployment.md#rate-limits)).

## Renaming services

Admins can change the identifier of a service (`POST
/services/{serviceId}/rename`, with the new identifier in the body, e.g.,
`{"id": "my-tes"}`; `200 OK`, with the new identifier). Everything stored
under the old identifier moves to the new one: the service, with its
[verification](#verified-services), [sync](#synced-services) and `createdAt`
(`updatedAt` is renewed),
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
slashes) or in roles (commas, line breaks), with `400 Bad Request` (see
[Errors](#errors)).

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
ignored, so clients can send services back as they got them, except for
admins: they can set
them, e.g., to keep the timestamps of services imported from another
registry. Those are stored as sent, so they may have other time zones or
precisions. `updatedAt` is only kept for services that admins register
(e.g., import); updates always set it. Timestamps that admins don't send
are set as for other users. Timestamps of admins must be RFC 3339
date-times (e.g., `2019-06-04T12:58:19Z`); others are rejected (`400 Bad
Request`), so admins who update a service with another stored `createdAt`
send a valid one, or none.
Timestamps that aren't date-times are rejected (`400 Bad Request`) for all
users. Verifying a service doesn't change its timestamps.

This differs from the GA4GH specification, where the timestamps describe
the deployment of the service (when it was first deployed, and last
updated): in the registry, they describe its entry.

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
  isn't registered returns `400 Bad Request` (code `unregistered-type`, see
  [Errors](#errors)), without quoting the type. Admins'
  services may have new types, which are registered.
- `moderated`: any authenticated user, but the types of users other than
  admins are pending until an admin approves them (see [Pending
  approvals](#pending-approvals)); admins' types are approved at once.

`GET /me` tells clients whether the current user may add types
(`mayAddServiceTypes`), e.g., to offer "Add new service type" only then,
and whether their new types are pending (`serviceTypesPending`); `GET
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
aren't registered (e.g., after a failed change of the registry's own entry).

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
  (`serviceTypes`) and services (`services`) the user may see: all for
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

Requests with invalid fields are rejected with `400 Bad Request`, listing
every invalid field in `errors`, so that clients can show each message at its
field, without validation rules of their own. That holds both for fields that
don't match the API specification (e.g., missing or unknown fields, values
that are too long, or don't match the `date-time` or `uri` format of their
field) and for those that the registry's own checks reject (e.g., URLs,
schemes, environments). The invalid fields that the checks find are listed
together, those of the body first, then the parameters (e.g., query parameters that an
operation doesn't declare, on all operations, such as `GET
/services/{serviceId}?page=1` and `GET /services?x=1`); the registry's own
checks run once a request matches the specification. Conflicts found first
(e.g., the [limit of unused types](#service-types), or a pending service) are
reported instead, as `409 Conflict`, and the address of
[imports](#importing-service-info) is reported at its first invalid part. The
format is that of
FOCA's [invalid
requests](https://foca.readthedocs.io/en/latest/problems/invalid-request/)
(RFC 9457), declared in the API specification (schemas `InvalidRequestError`
and `FieldError`):

```json
{
  "type": "https://foca.readthedocs.io/en/latest/problems/invalid-request/",
  "title": "Bad request",
  "status": 400,
  "detail": "'#/url': Must use one of these schemes: 'https'.; '#/environment': Must be one of: 'prod', 'test', 'dev', 'staging'.",
  "code": "invalid-request",
  "errors": [
    {
      "detail": "Must use one of these schemes: 'https'.",
      "pointer": "#/url",
      "code": "scheme-not-allowed"
    },
    {
      "detail": "Must be one of: 'prod', 'test', 'dev', 'staging'.",
      "pointer": "#/environment",
      "code": "invalid-value"
    }
  ]
}
```

Each entry has a readable message (`detail`), the field (`pointer`, a JSON
Pointer into the request body, or `parameter`, the name of a path or query
parameter) and a stable `code`, which clients branch on. The messages never
quote the rejected values, which may hold secrets (e.g., credentials in
URLs). `detail` is the reason of the rejection, as up to FOCA 0.21 (FOCA's
`exceptions.validation_reasons`, set in the app's `config.yaml`): for the
registry's own checks, the locations and messages of the invalid fields (as
above), which don't quote the values either; for the schema validation, the
validator's message, which may quote the rejected value (e.g., `'x' is not a
'uri'`). So show the messages of `errors` at their fields, not `detail`. With
`exceptions.validation_reasons: false` (e.g., in a [config
overlay](configuration.md)), `detail` names the invalid fields by location
and code instead, without values (e.g., `Invalid fields: #/url
(scheme-not-allowed); #/environment (invalid-value)`); FOCA logs them that
way in both cases. FOCA's schema validation reports generic codes (e.g., `required`,
`too-long`, `invalid-format`, `pattern-mismatch`, `unknown-property`,
`unknown-parameter`; see [FOCA's
codes](https://foca.readthedocs.io/en/latest/problems/invalid-request/#codes)),
the registry's own checks these:

| Code | Invalid field |
|---|---|
| `invalid-url` | the URL is invalid, e.g., has no host, user info, whitespace, or characters that URLs don't allow unencoded; `detail` says why (`url`, `contactUrl`, `documentationUrl`, `organization.url` of registrations, `url` of [verifications](#verified-services), `serviceInfoUrl` of [imports](#importing-service-info)) |
| `scheme-not-allowed` | the URL uses a scheme that its field doesn't allow, e.g., `http` for service URLs (see [Service URL schemes](configuration.md#service-url-schemes)), or other schemes than `http` and `https` for links (and `mailto` for `contactUrl`) and the `url` of [verifications](#verified-services) |
| `invalid-value` | the environment isn't one of those the deployment allows (`environment`; see [Service environments](configuration.md#service-environments)) |
| `invalid-format` | a timestamp isn't an RFC 3339 date-time (`createdAt`, `updatedAt`; see [Timestamps](#timestamps)); FOCA's schema validation reports most of these first, with the same code |
| `unregistered-type` | the service type isn't registered, and the user may not add types (`type`; see [Service types](#service-types)) |
| `unusable-id` | the identifier can't be stored in roles, e.g., with commas or line breaks (`serviceId` of `PUT /services/{serviceId}`, `id` of [renames](#renaming-services)) |
| `not-service-info-url` | the address doesn't end in `/service-info`, or has a query or fragment (`serviceInfoUrl` of [imports](#importing-service-info)) |
| `path-not-normalized` | the path of the address isn't in its normal form, e.g., has repeated slashes or dot segments (`serviceInfoUrl` of [imports](#importing-service-info)) |
| `too-long` | the address is too long (`serviceInfoUrl` of [imports](#importing-service-info)) |

Further codes may be added; show `detail` for unknown ones. Reserved
identifiers (`.`, `..`, `types`) and those with slashes don't match the
pattern of `serviceId` and of `id` of renames (`pattern-mismatch`). Other
`400` responses have neither `errors` nor `code`: bodies that aren't JSON or
have unpaired surrogates (e.g., `\ud800`), service info that doesn't pass the
checks of a registration ([imports](#importing-service-info) and
[syncs](#synced-services), whose `detail` names the field of the service
info), and the permissions endpoints.

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
| `pending-service` | the service is [pending](#pending-approvals): only admins update it, and it can't be verified; or the service type of the registry is pending, so it can't be [listed](#listing-the-registry-itself) |
| `unused-types-limit` | the user has added as many service types that no service has as allowed (see [Service types](configuration.md#service-types)) |
| `services-limit` | the user has registered as many services as allowed (see [Services per user](configuration.md#services-per-user)) |
| `service-type-registered` | the service type is registered, also if it is pending |
| `service-type-in-use` | services have the service type, so it can't be removed |
| `verification-mismatch` | the service has another URL or API type than the one checked (see [Verified services](#verified-services)) |
| `last-owner` | the subject is the last owner of the service |
| `invitations-limit` | the service has as many invitations that haven't expired as allowed; accepted ones count until they expire (see [Invitations](access-control.md#invitations)) |
| `invitation-accepted` | the invitation was accepted already (see [Invitations](access-control.md#invitations)) |
| `service-id-taken` | a service with the new identifier is registered (see [Renaming services](#renaming-services)), or with the identifier of the service info (see [Listing the registry itself](#listing-the-registry-itself)) |
| `service-busy` | the service, or the new identifier, is being renamed or changed (see [Renaming services](#renaming-services)) |
| `sync-url-locked` | the service has sync turned on, so its URL can't change (see [Synced services](#synced-services)) |
| `sync-unavailable` | the deployment doesn't sync, or the service is the registry's own entry (see [Synced services](#synced-services)) |
| `sync-identity-mismatch` | the service info has another identifier or API type than the service (see [Synced services](#synced-services)) |
| `conflict` | other conflicts (e.g., changes of policy rules set in the configuration via FOCA's permissions API) |

Further codes may be added; treat unknown ones like `conflict`. The API
specification documents the codes in the schema `ConflictError`.

See [Access control](access-control.md) for the roles and [Verified
services](#verified-services) for verifications. Extensions of the GA4GH
endpoints, such as pagination, search or filters, will be reconsidered with
the upcoming new version of the Service Registry specification.

## Importing service info

Importing is the recommended way to register a service: `POST /services/import`
fetches the service info of a service and returns the values for a registration,
which users can edit before registering them (`POST /services`). Nothing is
stored. It needs a login.

```bash
curl -X POST "http://localhost:8080/ga4gh/registry/v1/services/import" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"serviceInfoUrl": "https://tes.example.org/ga4gh/tes/v1/service-info"}'
```

- **Address:** `serviceInfoUrl` must end in `/service-info`, without a query or
  fragment, and use an [allowed scheme](configuration.md#service-url-schemes); `url`
  is set to it without `/service-info`. Other addresses are rejected (`400 Bad
  Request`, with the invalid field `serviceInfoUrl`, see [Errors](#errors)),
  without fetching them.
- **Values:** the response has `name`, `type`, `description`, `organization`
  (`name`, `url`), `contactUrl`, `documentationUrl`, `environment`, `version` and
  `url`, as far as the service info has them; other fields are ignored.
- **Checks:** service info needs an `id`, and the values are checked as in a
  registration (`400 Bad Request`, naming the field but not its value). Whether a
  service is registered already, and whether the user may add the type, is
  checked on `POST /services`.

**Only publicly available service info can be imported:** the registry never
sends a token. We strongly recommend that services make their service info public,
as it tells people how to get access.

The registry fetches addresses that users choose, so it takes these precautions
against server-side request forgery:

- it connects to public addresses only, unless the deployment [permits
  networks](configuration.md#service-info-import). All addresses of a host must be
  allowed; they are checked after they are resolved, and the connection goes to a
  checked address, so that a name can't point elsewhere afterwards;
- `https` only, unless the deployment [allows
  `http`](configuration.md#service-url-schemes), with the certificate checked for
  the host;
- no redirects, at most 256 KiB, at most 10 seconds;
- if a host has several addresses, they are tried in turn, IPv6 and IPv4
  alternating, and each gets an equal share of the time left to connect (including
  the TLS handshake), so that an address that doesn't answer (e.g., IPv6
  connections that are dropped on the way) leaves time for the others;
- users may have service info fetched 30 times per hour by default, and only few
  requests fetch at a time (`429 Too Many Requests`, with `Retry-After`, which browser
  apps on other origins can read as well; see [Service info
  import](configuration.md#service-info-import));
- every failed fetch returns the same `502 Bad Gateway`, whatever the cause
  (e.g., an address that isn't allowed, an unknown host, a timeout or a status
  other than `200`), so that users can't map networks with it.

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
other than `http` and `https`, user info (`user:password@`), whitespace, control
or format characters are rejected (`400 Bad Request`, saying why).

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
[Verified services](#verified-services)). Proving control of a URL by fetching
`{url}/service-info` was rejected, and stays so: service info is self-declared,
so serving it proves nothing about who registers the entry. The registry does
fetch service info, but for another purpose, with safeguards against server-side
request forgery: to [import](#importing-service-info) what a service says about
itself, and to [keep the entry up to date](#synced-services).

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
{"url": "https://tes.example.org/ga4gh/tes/v1", "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}, "verifiedAt": "2026-10-20T12:34:56.789Z"}
```

Verifying a service again renews it. `DELETE` on the same path removes it.
Both are for admins only, and return `404 Not Found` for unknown services
(`DELETE` also for unverified ones).

Verifications can be read by anyone (unless the deployment [requires
login](access-control.md#requiring-login-for-reads)):
`GET /services/<id>/verification`
returns the verification of a service (`404 Not Found` if it is not
verified), and `GET /verifications` lists all verified services, sorted by
identifier, each with its identifier as `serviceId` and the fields above.

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

## Synced services

Owners and maintainers of a service, and admins, can have the registry keep it up
to date with its service info. Sync is recommended for imported services: entries
without it can drift from what the service says.

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/sync" \
  -H "Authorization: Bearer <token>"
```

`PUT` fetches the service info at `{url}/service-info` with the [same
safeguards](#importing-service-info) as imports (never with a token), applies it,
and turns sync on; repeating it syncs the service at once. The registry then
syncs it every 24 hours by default (see [Service info
sync](configuration.md#service-info-sync)), whether or not requests come in. With
`?onlyIfEnabled=true`, `PUT` only syncs a service that has sync on, and otherwise
returns `404 Not Found` without a fetch: use it for "sync now", which can't then turn
on what someone just turned off. `DELETE` turns it off. The service info must be
publicly available: a service whose
service info isn't world-readable can't be synced, so its entry can't be protected
from drift. We strongly recommend that services make their service info public,
also as it tells people how to get access.

**What sync changes:** `name`, `type`, `description`, `organization`, `contactUrl`,
`documentationUrl`, `environment` and `version`, removing optional ones that the
service info no longer has. Never the identifier, `url` or the roles. `updatedAt`
only changes if a value does. The [verification](#verified-services) stays: it says
that an admin knows and checked the registrants, and a service can change right
after any check, with or without sync. Changes with `PUT /services/{serviceId}` are
allowed, but last until the next sync, as sync overrides them.

**The URL stays:** while sync is on, `PUT /services/{serviceId}` with another `url`
(as written) returns `409 Conflict` (code `sync-url-locked`), also for admins. To
move a service, turn sync off first, or import and register it anew.

**The state** can be read by anyone (unless the deployment [requires
login](access-control.md#requiring-login-for-reads)): `GET
/services/{serviceId}/sync` returns it (`404 Not Found` if the service has no sync),
and `GET /syncs` lists all services with sync, each with `serviceId`, sorted by
identifier; `?status=synced` or `?status=out-of-sync` selects by the label.

```json
{"url": "https://tes.example.org/ga4gh/tes/v1", "type": {"group": "org.ga4gh", "artifact": "tes", "version": "1.1.0"}, "status": "out-of-sync", "reason": "unreachable", "serviceInfoId": "org.example.tes", "lastAttemptAt": "2026-11-05T12:34:56.789Z", "lastSuccessAt": "2026-11-03T12:34:56.789Z"}
```

Like [verification](#verified-services), the label isn't served with the GA4GH
endpoints. A service is in one of three states:

- **no sync** (`404 Not Found`, not listed): a manual entry, which may have drifted;
- **`synced`**: the last sync succeeded, at most two intervals ago, so that a
  registry that stopped syncing can't show `synced` forever;
- **`out-of-sync`**: the last sync failed, with a `reason`, or the last success is
  too long ago, without one: syncs aren't running, so tell the operators of the
  registry. Together with the times, this tells admins whom to contact.

If a sync fails, the service keeps its last values. The reasons are a fixed set,
never network errors:

| `reason` | Meaning |
| --- | --- |
| `unreachable` | the fetch failed, also after two retries within minutes (only then does the label change): the service is down, requires a token, redirects, or the response is too large or too slow |
| `address not allowed` | the host resolves to an address that isn't public |
| `invalid service info` | it doesn't pass the checks of a registration (e.g., an [environment](configuration.md#service-environments) that isn't allowed) |
| `identity mismatch` | it has another `id` than when sync was turned on, or another `type.group` or `type.artifact` than the service: something is seriously wrong, and no type is added. If a new `id` is right, turn sync off and on again |
| `version awaiting approval` | [mode `moderated`](#pending-approvals): its `type` is new, and pending (listed in `GET /approvals`); the service stays listed with its old values, and the next sync after the approval applies it. It counts for the limit of unused types of the user who registered the service |
| `version not allowed` | mode `controlled`, or a limit of unused types of `0`: its `type` isn't registered, and only admins may add it. Ask them to |
| `unused types limit reached` | the user who registered the service, or this service if an admin registered it, has added [as many unused types as allowed](configuration.md#service-types) |

In mode `open`, a new `type` is added, for the user who registered the service, and
applied. Service info is self-declared, so these rules also hold for services that
admins registered; as these count for nobody, each has a limit of its own. Versions
that a sync leaves behind, if no other service has them, no longer count, so that
releasing new versions doesn't use up the limit; they stay until admins remove them.

**Turning it on** changes nothing if the first fetch fails (the same `502 Bad
Gateway` as for imports, whatever the cause, so that users can't map networks, also
counting for the [limit](configuration.md#service-info-import)), the service info is
invalid (`400 Bad Request`) or describes another service (`409 Conflict`, code
`sync-identity-mismatch`). `409 Conflict` with code `sync-unavailable` means that the
deployment doesn't sync, or that the service is the registry's [own
entry](#listing-the-registry-itself), and code `service-busy` that the service changed
during the fetch; try again.

**Verified and synced doesn't mean trusted.** Verified says that an admin checked
something once, and synced that a service recently described itself in a way the
registry could read: both are facts about the entry, not about the service. Service
info is self-declared, and a service can change at any time. Users decide whom they
trust. Clients on the safe side use services that are both verified and synced: they
list the services (`GET /services`) and keep those listed by `GET /verifications` and
`GET /syncs?status=synced` with the same identifier, `url` and `type` (`group` and
`artifact`).

## Notifications

`GET /notifications` tells a logged-in user what needs their attention, e.g., for a
client to show. There is no inbox: notifications are computed from the stored data
when they are read, so that they end with what caused them. The registry doesn't
send any.

```json
[{"id": "3f2a9c1e5b7d4f608a1c2e3d4b5a6978", "type": "out-of-sync", "serviceId": "ABC123", "reason": "unreachable", "since": "2026-11-05T12:34:56.789Z"}]
```

Each has a `type`, the `serviceId` (or a `serviceType`, for pending types) to link
to, a `reason`, and `since` as far as known; the most recent come first.

| `type` | Who sees it | About | `reason` |
| --- | --- | --- | --- |
| `out-of-sync` | users with a role on the service, admins in admin mode | a service that is [out of sync](#synced-services) | one of those of syncs, or `no recent sync` if none succeeded in time, without any failing: syncs aren't running |
| `pending-service-type` | admins in admin mode | a type that [waits for approval](#service-types) | `awaiting approval` |
| `pending-service` | admins in admin mode | a service that waits for approval | `awaiting approval` |
| `environment-not-allowed` | admins in admin mode | a service with an [environment](configuration.md#service-environments) that is no longer allowed | `environment not allowed` |
| `url-scheme-not-allowed` | admins in admin mode | a service with a URL scheme that is no longer allowed | `url scheme not allowed` |

Without [admin mode](access-control.md#admin-mode), admins see what other users see.

`DELETE /notifications/{notificationId}` dismisses one: users with a role on the
service dismiss it for each other, and admins for all admins, but not for each other.
It returns the identifier, or `404 Not Found` if the user has no such notification
(anymore). A dismissed notification only comes back if the problem ended and came
back, or its reason changed. Updates and renames keep it. For what only admins see, the
registry notices that a problem ended when an admin lists notifications meanwhile: if
none did, a service back on the same pending type, or a configuration that changed back
and forth, stays dismissed. `GET /approvals` always lists what is pending. When admins
list notifications in admin mode, the registry also removes the stored dismissals, of
owners and admins alike, of problems that ended and of services that were deleted;
until then, they stay stored.

## Listing the registry itself

The registry doesn't list itself among its services by default. Admins can add
it, so that clients find it like any other service, or remove it again. These
operations are for admins only, in [admin
mode](access-control.md#admin-mode) (`403 Forbidden` otherwise):

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/service-info/listing" \
  -H "Authorization: Bearer <admin token>" -H "X-Request-Roles: admin"
```

| Request | Response |
| --- | --- |
| `GET /service-info/listing` | `200 OK`, the state |
| `PUT /service-info/listing` | `201 Created`, the state, and the URL of the entry in `Location`, if the registry was added; `200 OK` and the state if it was listed, with its entry brought up to date |
| `DELETE /service-info/listing` | `200 OK`, the state; `404 Not Found` if the registry isn't listed; `409 Conflict`, code `service-busy`, while the entry is being [renamed](#renaming-services) |

The state says whether the registry is listed, and if so, under which
identifier: `{"listed": true, "serviceId": "org.example.cloud-registry"}` or
`{"listed": false}`.

**The entry** is a service with the fields of the [service
info](configuration.md#service-info) (`name`, `type`, `description`,
`organization`, `contactUrl`, `documentationUrl`, `environment`, `version`) and
its identifier (`id`). Its `url` is the [public URL](configuration.md#public-urls)
of the app with the base path of the API, e.g.,
`https://registry.example.org/ga4gh/registry/v1`, so set any path prefix in the
public URL. The registry sets its timestamps as for other services. It has no
owner, doesn't count for the [limit of services of any
user](configuration.md#services-per-user) and is
[verified](#verified-services). `GET /services` and the other GA4GH endpoints
serve it like any other service, and the service info is unchanged.

**The service type** of the entry is added if it is missing, whatever the mode
of [service types](#service-types), and is approved. `DELETE` also removes the
type, unless other services have it, whoever added it.

**Refused requests** change nothing, and return, saying why:

- `400 Bad Request`: the environment isn't one of the [allowed
  values](configuration.md#service-environments), the scheme of the public URL
  isn't an [allowed scheme](configuration.md#service-url-schemes) (e.g., an
  `http` URL, by default), or the identifier or a URL of the service info can't
  be used for services;
- `409 Conflict`, code `duplicate-service`: another service has the same URL
  and API type, e.g., the registry registered by hand (named in `serviceId`).
  The entry doesn't take it over: delete that service first;
- `409 Conflict`, code `service-id-taken`: another service has the identifier
  of the service info: rename or delete it, or change the identifier;
- `409 Conflict`, code `pending-service`: the service type of the registry is
  [pending](#pending-approvals). Pending services can't be verified, and
  listing the registry doesn't approve the type, as that would also approve the
  services of other users that have it: approve it first;
- `409 Conflict`, code `service-busy`: the entry is being
  [renamed](#renaming-services); try again.

**While it is listed,** the app brings the entry up to date when it starts, so
that it follows the service info and the public URL (e.g., its `version` follows
the version of the app), and writes it only if it differs. `updatedAt` changes
then. The verification stays, and its time is renewed if the URL or API type
changes. A new type of the entry is added; the former type stays, as when other
services change their types. If the entry can't be brought up to date for one of
the reasons above, it stays as it is, and the app logs a warning. `PUT` does the
same, but returns the error.

The entry can be managed like other services, within these limits:

- [Renaming](#renaming-services) keeps it listed, and the identifier isn't changed
  back, also if `service_info.service.id` changes. To use a new identifier,
  rename the entry, or remove and add it.
- Changes of its fields (`PUT /services/{serviceId}`), and removing its
  verification, only last until the app next starts or `PUT` is repeated.
- The service info isn't checked against the [maximum lengths of
  registrations](#registrations), so sending an entry with longer fields back
  with `PUT /services/{serviceId}` is refused (`400 Bad Request`).
- Deleting it (`DELETE /services/{serviceId}`) ends the listing, but leaves its
  type, as for other services: use `DELETE /service-info/listing` instead.

Adding and removing are one operation each, without transactions (which need a
replica set). If adding fails, a type it added is removed again. If the app stops
while adding, repeat the operation. If removing fails or the app stops after the
entry was deleted, its type may stay, without services: remove it (`DELETE
/services/types`). The app adds the type of an entry without one when it starts.
