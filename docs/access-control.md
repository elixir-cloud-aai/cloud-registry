# Access control

Services and service info can be read by anyone, unless the deployment
[requires login for reads](#requiring-login-for-reads). Writing requires an
access token of an OpenID Connect provider that the deployment trusts (see
[Authentication](#authentication)). The identity provider, its claims and the
groups mapped to roles are deployment configuration, without defaults (see
[Deployment configuration](configuration.md#deployment-configuration)).
Access control is implemented with
[FOCA](https://foca.readthedocs.io/en/latest/access-control/); see the
`security` section of the
[configuration](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml).

## Authentication

Read operations (`GET`, except `/services/{serviceId}/permissions` and `/me`,
see the [table of endpoints](api.md)) are public by default (see [Requiring
login for reads](#requiring-login-for-reads)). All other operations require
an access token of a trusted issuer, sent as a bearer token:

```bash
curl -X POST "http://localhost:8080/ga4gh/registry/v1/services" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d @service.json
```

Requests without a valid token are rejected with `401 Unauthorized` and a
Bearer challenge (`WWW-Authenticate: Bearer`, RFC 6750; with
`error="invalid_token"` for invalid tokens); requests with a valid token, but
without the required role, with `403 Forbidden`.

Tokens are obtained by logging in with the identity provider through an app
registered as its client (OpenID Connect), e.g., the registry's [front
end](https://github.com/elixir-cloud-aai/cloud-registry-ui), or a client of
your own for scripts. The registry accepts tokens only:

- that are access tokens as per [RFC
  9068](https://www.rfc-editor.org/rfc/rfc9068) (`typ` header `at+jwt`);
  other tokens, such as ID tokens, are rejected;
- of the issuers in `security.auth.issuers` (none by default: each
  deployment sets its identity provider);
- for the client IDs in `security.auth.audience` (the `aud` claim), which each
  deployment sets to the apps that call it (see
  [Configuration](configuration.md#deployment-configuration)); the Docker
  Compose setup accepts tokens for any audience.

The registry reads the user's group memberships from the group claim
(`security.access_control.claim_groups`, needed for the [admin role](#admins)
and [roles of groups](#roles-of-groups)) and their name (`name`). Claims in
`security.auth.userinfo_claims` are read from the identity provider's user
info endpoint with the token, for identity providers that don't put them in
access tokens, so the token must be allowed to release them (e.g., request the
corresponding scopes when logging in). Tokens are validated by
[FOCA](https://foca.readthedocs.io/en/latest/authentication/).

## Requiring login for reads

Deployments can require login for all reads, except `GET /service-info`, with
a config overlay, using FOCA's [spec
patches](https://foca.readthedocs.io/en/latest/configuration/api/#patching-specifications)
and [named
entries](https://foca.readthedocs.io/en/latest/configuration/#named-entries).
The app ships a patch for this,
[`cloud_registry/api/patches/login-for-reads.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/api/patches/login-for-reads.yaml),
which sets the security requirements of the public read operations to a
bearer token:

```yaml
--8<-- "cloud_registry/api/patches/login-for-reads.yaml"
```

Apply it to the registry's API specification (named `registry` in the base
config) in an overlay (the path is relative to the directory of the base
config, where the patch is installed):

```yaml title="overlay.yaml"
api:
    specs:
        registry:
            patches:
                - api/patches/login-for-reads.yaml
```

With the Helm chart, put the same in the `config` value. Requests to the
patched reads without a valid token are then rejected with `401
Unauthorized`; any authenticated user may read, by the default policies
(in `security.access_control.policies`, which are only evaluated for reads
that require login): `list-services`, `get-service` (also for
`/services/types`, which matches `/services/:id`), `get-service-verification`
and `list-verifications`. `GET /service-info`, the API
specification (`/openapi.json`) and the Swagger UI (`/ui/`) stay public.

If an overlay of the deployment gives the policies as a list, which replaces
the rules of the base config, add these rules to the list (FOCA refuses named
rules after a list, see [Configuration](configuration.md#deployment-configuration)).

To allow reads for some users only, replace these rules by name in the
overlay, e.g., with rules for a role that a group is mapped to, in addition to
the deployment's admin groups (see [Admins](#admins) for `group_mapping`;
`null` removes a rule):

```yaml title="readers.yaml"
security:
    access_control:
        group_mapping:
            registry-readers: reader
        policies:
            list-services: [p, reader, /ga4gh/registry/v1/services, GET]
            get-service: [p, reader, "/ga4gh/registry/v1/services/:id", GET]
            get-service-verification: null
            list-verifications: null
```

Readers may then list and get services and get the service types, but not
read verifications; admins may read everything.

Owners and maintainers of services can still update them, but only read them
with a rule that allows them to.

Registration stays open to any authenticated user (`register-services`), and
services are unique by URL and API type: registering a service again, or
changing one to the URL and API type of another one, returns `409 Conflict`.
It names the registered service (`serviceId`, also in `detail`) only to
users who may read it, by the same decision as for
`GET /services/{serviceId}` (here: readers and admins); other users get a
`409` without it. They can still find out whether a service with a given URL and
API type is registered (`409` rather than `201`), which can't be avoided
while duplicates are rejected. To prevent that, restrict registration, too, by
adding a rule of the same name to the policies of `readers.yaml` above, e.g.,
for the readers' role, or `null` for admins only:

```yaml
            register-services: [p, reader, /ga4gh/registry/v1/services, POST]
```

This only stops new registrations: owners and maintainers can still find out
the same by changing one of their services to the URL and API type of
another one. Owners grant these roles to any user or group (see [Roles on
services](#roles-on-services)), and users who registered services while
registration was open keep theirs. So admins should review the roles on
services when restricting reads (`GET /services/<id>/permissions`), and
deployments that don't want owners to grant roles can restrict
`owners-change-permissions` and `owners-change-group-permissions` by name
in the same overlay, too.

!!! danger "With great power comes great responsibility"
    Patches and rules in overlays can weaken or remove authentication and
    access control as easily as they add them. Review them like code, keep
    them as small as possible, and check the startup logs, which list the
    applied patches and warn of operations that patches make public (see
    FOCA's [patching
    specifications](https://foca.readthedocs.io/en/latest/configuration/api/#patching-specifications)).

## Roles on services

- Any authenticated user can register a service (`POST /services`) and
  becomes its **owner**.
- Owners can update (`PUT`) and delete (`DELETE`) their services, and grant
  other users the roles `owner` or `maintainer` on them, by their user
  identifier (`sub` claim):

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
  service instead. The role `maintainer` is only granted on services with an
  owner (`400 Bad Request` otherwise, e.g., on services stored before owners
  were introduced: admins grant `owner` first).
- **Maintainers** can update, but not delete a service.
- [Admins](#admins) can do everything.

Roles are granted, revoked and listed with FOCA's [resource ownership
helpers](https://foca.readthedocs.io/en/latest/access-control/#resource-ownership),
for the roles in `security.access_control.resource_roles` (`owner` and
`maintainer`; don't change them, as the API and the policies are written for
them). Groups must not be mapped to these roles
(`security.access_control.group_mapping`), as all members of such a group
would have the role on all services; FOCA refuses to start then.
FOCA's service clients (`security.auth.service_clients`,
`security.auth.on_behalf_of_header`) are not supported: the app refuses to
start with them, as FOCA would make the clients (or the users they name)
owners, which the registry's endpoints neither list nor manage.

## Roles of groups

Owners can also grant these roles to **groups** they are members of, by the
group (a value of the group claim, e.g., a group of the team that runs the
service), URL-encoded in the path; all members of the group then have the
role, so team changes are handled centrally in the identity provider instead
of per service:

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/permissions/groups/urn%3Aexample%3Agroup%3Aregistry-team%23login.example.org" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"role": "owner"}'
```

Granting roles to groups the owner is not a member of is refused (`403
Forbidden`), as owners could otherwise hand a service to any group; admins can
grant roles to any group. Any owner can revoke the roles of a group
(`DELETE /services/<id>/permissions/groups/<group>`).
`GET /services/<id>/permissions` lists the roles of groups together with those
of users, as `{"group": <group>, "role": <role>}` (users: `user_id`,
`name`, `role`). Groups count as owners: the last user owner can be removed
while a group is owner, and the last owner group can't be removed or
downgraded (`409 Conflict`). A service whose only owner is a group then
depends on the group's membership in the identity provider: if the group is
emptied or
deleted, only admins can manage the service. Roles of groups apply because
the base config sets `security.access_control.keep_group_subjects` (all values
of the group claim are used as subjects, besides the admin group mapping);
don't unset it in overlays (the app warns on startup if it is unset, and roles
can't be granted to groups then: `400 Bad Request`).

!!! warning
    All members of an owner group are owners: they can list the identifiers
    and names of all users with roles on the service
    (`GET /services/<id>/permissions`), and remove the other owners, users
    and groups. Don't grant roles to broad groups, e.g., to all members of a
    virtual organization (VO) or of a large community, but to groups of the
    team that runs the service.

### Group membership changes

Group memberships are those of the token: if the group claim is in the access
token, users removed from a group keep its roles, also the role `admin`, until
the token expires. If it is read from the user info
(`security.auth.userinfo_claims`), FOCA caches the user info per token for up
to `security.auth.cache_ttl` seconds (default: 300, but not beyond the
token's expiry; per worker process). So users removed from a group keep its
roles for up to 5 minutes, and users added to a group get them only after
that time (or with a new token).
To shorten the delay, lower `cache_ttl` (which also sets how long the
issuers' discovery documents and keys are cached), or set
`security.auth.validate_userinfo: true`, which fetches the user info for
every request with a token (and so also rejects revoked tokens), at the cost
of a request to the identity provider each time (see FOCA's
[authentication](https://foca.readthedocs.io/en/latest/authentication/)).

## Admins

Members of the groups that the deployment maps to the role `admin`
(`security.access_control.group_mapping`) are **admins**: they can do
everything, including registering services with chosen identifiers (`PUT` of
a new service) and [verifying services](api.md#verified-services). E.g.,
with the group claim `groups`:

```yaml
security:
    access_control:
        claim_groups: groups
        group_mapping:
            registry-admins: admin
```

On startup, the app logs the groups mapped to `admin`. Admins can also make
other users admins (see [Operations](operations.md#more-admins)).

## Current user

`GET /me` returns the current user's identifier (to give to owners), whether
they are an admin, and their roles on services, also those through groups
(with `group`; a service is listed once per role); it also stores their name
for the services with roles granted to them. Owners are not shown in the
public service responses.
