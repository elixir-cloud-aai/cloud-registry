# Operations

Tasks of the admins of a deployment (see [Admins](access-control.md#admins))
and the logs that record them. Admins also [verify
services](api.md#verified-services) and resolve conflicts over [duplicate
services](api.md#duplicate-services).

## Audit logs

For auditing, the app logs (at level `INFO`) who changed which roles of whom
(users or groups) on which service, who deleted services, and who verified
services or removed their verifications (also by updating their URL or API
type). Changes via the [permissions API](#permissions-api) are not logged by
the app itself: FOCA logs the requests (method, path and client address) and
the identifiers of the added, updated or deleted policy rules, but not who
changed them, or how.

## Permissions API

FOCA also serves its permissions API at `/admin/access-control/permissions`
(not part of the registry's API specification), which lists, adds, changes and
deletes the stored policy rules directly. Only admins can use it. Use the
registry's endpoints (see [Access control](access-control.md)) to manage roles
on services: the permissions API bypasses the registry's checks, e.g., it can
remove the last owner of a service (without `409 Conflict`) and leaves the
stored names of users (see `GET /services/{serviceId}/permissions`) in place.
The configured policies (`security.access_control.policies`) are managed on
startup and can't be changed via the API.

### More admins

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
the internet, route only `/ga4gh/registry/v1/` through a reverse proxy (see
[Ingress](deployment.md#ingress) for the Helm chart).
