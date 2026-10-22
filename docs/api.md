# API

The endpoints of the [GA4GH Service Registry
API](https://github.com/ga4gh-discovery/ga4gh-service-registry) (version
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
| `/services/{serviceId}/permissions/groups/{entitlement}` | `PUT`, `DELETE` | owners, admins (`PUT` by owners: groups they are members of) |
| `/services/{serviceId}/verification` | `GET` | anyone |
| `/services/{serviceId}/verification` | `PUT`, `DELETE` | admins |
| `/verifications` | `GET` | anyone |
| `/me` | `GET` | any authenticated user |

The [Swagger UI](getting-started.md#quick-start) of a deployment documents
all operations and serves the API specification
(`/ga4gh/registry/v1/openapi.json`).

## Registrations

Registering a service (`POST /services`, or `PUT /services/{serviceId}` with a
new identifier) returns `201 Created`, with the identifier of the service in
the body and its [public URL](configuration.md#public-urls) in the `Location`
header; updating a service (`PUT`) returns `200 OK`. Only admins can register
services with `PUT`: others can only update services they have a role on
(`403 Forbidden` otherwise). Services deleted while they are registered or
updated return `404 Not Found`, unless an admin's `PUT` registers them again.
Identifiers `.` and `..` (which clients resolve in URLs) and `types` (the path
of `GET /services/types`) are rejected (`400 Bad Request`).

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

Timestamps in registrations (`createdAt`, `updatedAt`) must be RFC 3339
date-times (e.g., `2019-06-04T12:58:19Z`); others are rejected (`400 Bad
Request`).

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

Services stored before the checks of timestamps and lengths were introduced
may not pass them; see [Upgrading](upgrading.md#stored-services).

The number of registrations per user is not limited, so deployments should
limit request rates at the ingress (see [Rate
limits](deployment.md#rate-limits)).

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

Services stored before this check are checked on startup (see
[Upgrading](upgrading.md#stored-services)).

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
- Changing them back doesn't restore it. Other updates (e.g., of `version`,
  `type.version` or `name`) keep it.
- Deleting a service deletes its verification.
- Admins should send the URL and API type they checked when verifying (see
  above), so that a service changed meanwhile isn't verified.
