# Access control

Services and service info can be read by anyone. Writing requires an access
token issued by [LS Login](https://lifescience-ri.eu/ls-login/) (see
[Authentication](#authentication)). Access control is implemented with
[FOCA](https://foca.readthedocs.io/en/latest/access-control/); see the
`security` section of the
[configuration](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml).

## Authentication

Read operations (`GET`, except `/services/{serviceId}/permissions` and `/me`,
see the [table of endpoints](api.md)) are public. All other operations require
an access token issued by LS Login, sent as a bearer token:

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
end](https://github.com/elixir-cloud-aai/cloud-registry-ui), or a client of
your own for scripts. The registry accepts tokens only:

- of the issuers in `security.auth.issuers` (LS Login by default);
- for the client IDs in `security.auth.audience` (the `aud` claim), which each
  deployment sets to the apps that call it (see
  [Configuration](configuration.md)); the Docker Compose setup accepts tokens
  for any audience.

The registry reads the user's group memberships (`eduperson_entitlement`,
needed for the [admin role](#admins)) and name from LS Login's user info
endpoint with the token, so the token must be allowed to release them (e.g.,
request the corresponding scopes when logging in). Tokens are validated by
[FOCA](https://foca.readthedocs.io/en/latest/authentication/).

## Roles on services

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
- [Admins](#admins) can do everything.

## Roles of groups

Owners can also grant these roles to **groups** they are members of, by the
group's entitlement (a value of `eduperson_entitlement`, e.g., a group of an
ELIXIR node's team in Perun), URL-encoded in the path; all members of the
group then have the role, so team changes are handled centrally in Perun
instead of per service:

```bash
curl -X PUT "http://localhost:8080/ga4gh/registry/v1/services/<id>/permissions/groups/urn%3Ageant%3Alifescience-ri.eu%3Agroup%3Aelixir%3A<group>%23aai.lifescience-ri.eu" \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"role": "owner"}'
```

Granting roles to groups the owner is not a member of is refused (`403
Forbidden`), as owners could otherwise hand a service to any group; admins can
grant roles to any group. Any owner can revoke the roles of a group
(`DELETE /services/<id>/permissions/groups/<entitlement>`).
`GET /services/<id>/permissions` lists the roles of groups together with those
of users, as `{"group": <entitlement>, "role": <role>}` (users: `user_id`,
`name`, `role`). Groups count as owners: the last user owner can be removed
while a group is owner, and the last owner group can't be removed or
downgraded (`409 Conflict`). A service whose only owner is a group then
depends on the group's membership in Perun: if the group is emptied or
deleted, only admins can manage the service. Roles of groups apply because
the base config sets `security.access_control.keep_group_subjects` (all values
of the group claim are used as subjects, besides the admin group mapping);
don't unset it in overlays (the app warns on startup if it is unset).

## Admins

Members of the groups that the deployment maps to the role `admin`
(`security.access_control.group_mapping`) are **admins**: they can do
everything, including registering services with chosen identifiers (`PUT` of
a new service) and [verifying services](api.md#verified-services). For
ELIXIR, that is the LS Login group `ECP_CLN:ADMIN`:

```yaml
security:
    access_control:
        group_mapping:
            "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu": admin
```

On startup, the app logs the groups mapped to `admin`. Admins can also make
other users admins (see [Operations](operations.md#more-admins)).

## Current user

`GET /me` returns the current user's identifier (to give to owners), whether
they are an admin, and their roles on services, also those through groups
(with `group`; a service is listed once per role); it also stores their name
for the services with roles granted to them. Owners are not shown in the
public service responses.
