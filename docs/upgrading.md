# Upgrading

The [release notes](changelog.md) list the breaking changes of each release,
e.g., [of 0.4.0](releases/0.4.0.md#breaking-changes), and link the upgrade
steps on this page. Upgrades of Helm releases are described in
[Deployment](deployment.md#upgrades). This page describes how to upgrade, and
how existing setups and data are affected.

## From 0.6.0

Data is kept. Owners can invite users to their services by link (see
[Invitations](access-control.md#invitations)):

- The app stores invitations in the new collection `invitations`, whose
  indexes it creates on startup (one of them a TTL index, with which MongoDB
  deletes expired invitations).
- The base config has three new rules (`owners-manage-invitations`,
  `owners-revoke-invitations`, `accept-invitations` in
  `security.access_control.policies`). Overlays that give the policies as a
  list, rather than changing single rules by name, must add them, or the new
  endpoints return `403 Forbidden`.
- Deployments that stopped owners from granting roles, by removing or
  restricting `owners-change-permissions` by name (see [Requiring login for
  reads](access-control.md#requiring-login-for-reads)), inherit the rules of
  invitations, with which owners can grant `owner` and `maintainer` again:
  set `owners-manage-invitations` and `owners-revoke-invitations` to `null`
  in the same overlay. The app warns on startup if owners can create
  invitations, but no rule lets them grant roles to users.
- The new options `custom.access_control.invitations` (`expiry_days`, at
  most 3650, and `max_per_service`) have defaults (see
  [Invitations](configuration.md#invitations)).

Users other than admins may now register at most 50 services each
(`custom.access_control.max_services_per_user`; see [Services per
user](configuration.md#services-per-user)): check how many services your
users registered, and raise the limit, or set `null` to turn it off, in the
same rollout if needed. Users who registered more keep their services, but
can't register others until they are below the limit. After the upgrade,
list them, e.g., with `mongosh` (see [MongoDB](deployment.md#mongodb); with
your limit instead of 50):

```javascript
db.getSiblingDB("serviceStore").service_counts.find({count: {$gte: 50}})
```

The app counts each service for the user who registered it, stored with the
service from now on. On startup, it adds that user to the services stored
before: the only user who is `owner` of the service (services with no or
several such owners, e.g., owned by a group, count for nobody), and counts the
services per user (new collection `service_counts`, and a new index of
`services`). Restart the app once after the rollout (or upgrade with the app
scaled to 0 replicas), so that services registered, updated or deleted by
pods of 0.6.0 during the rollout are counted correctly.

Services that admins registered before the upgrade have the admin as their
only owner, so they count for the admin, unlike those that admins register
in admin mode from now on. The app can't tell admins apart on startup (they
are admins through the groups in their tokens), so if an admin registered
many services (e.g., imported them), make these count for nobody after the
upgrade (with the admin's user identifier), and restart the app, so that it
corrects the counters:

```javascript
db.getSiblingDB("serviceStore").services.updateMany(
  {_createdBy: "<admin's user identifier>"},
  {$set: {_createdBy: null}},
)
```

Registrations and updates (`POST /services`, `PUT /services/{serviceId}`)
with organization fields other than `name` and `url` are now rejected (`400
Bad Request`; see [Registrations](api.md#registrations)). Clients that send
further fields in `organization` must drop them. Services stored with such
fields are still served; the app warns of them on startup (see [Stored
services](#stored-services)).

With the Helm chart, the default `cloudRegistry.image` is now fully
qualified, `docker.io/elixircloud/cloud-registry` (see
[Image](deployment.md#image)): adapt image allow-lists, admission policies
(e.g., of Kyverno or Gatekeeper) and registry mirror rules that match the
short name `elixircloud/cloud-registry`, or pods may be rejected, or pull
past the mirror, after `helm upgrade`.

## From 0.5.0

Data is kept. The app now builds on FOCA 0.18, which provides several of
its settings and checks; change the config before upgrading:

1. **Public URL:** `custom.endpoints.service` is removed: set FOCA's
   `server.public_url` to the absolute URL of the app as clients see it,
   including any path prefix of a proxy (formerly `api_path`), e.g.,
   `https://registry.example.org/` instead of `url_prefix: https`,
   `external_host: registry.example.org` and `external_port: 443` (see
   [Public URLs](configuration.md#public-urls)). The app refuses to start
   without it, and FOCA refuses URLs with a query or fragment. With the Helm
   chart, `hostName` keeps working (`https://<hostName>/`); set `publicUrl`
   for anything else (see [Deployment](deployment.md#upgrades)).
2. **Admins required:** `custom.access_control.require_admin` moves to
   FOCA's `security.access_control.require_admin` (`true` in the base
   config, as before); the old key is rejected. The other settings of
   `custom.access_control` (`service_types`, `max_unused_service_types`)
   stay.
3. **Gunicorn:** the image loads FOCA's settings (`foca.gunicorn_config`);
   `cloud_registry.gunicorn_config` no longer exists. The settings are the
   same; custom commands that load the old module must load FOCA's.
4. **Admin mode:** admins act as admins only in requests that ask for it
   (header `X-Request-Roles: admin`; see [Admin
   mode](access-control.md#admin-mode)). Tell the maintainers of clients
   and scripts that need admin rights to send the header, or remove the
   opt-in (`security.access_control.opt_in_roles: []`); for web UIs, see
   [Admin mode](access-control.md#admin-mode). In `GET /me`, `admin` is
   now true only in admin mode; clients decide whether to offer the switch
   by the new field `admin_available`. FOCA refuses
   configured rules that grant the role `admin`
   (`security.access_control.policies`) on startup. Stored ones (e.g.,
   granted via the permissions API) still apply, without admin mode, and
   FOCA warns of them when policies are first loaded. Revoke them (`DELETE
   /admin/access-control/permissions/<rule-id>`, in admin mode), and add
   the users to an admin group instead. During a rolling update, pods of
   the previous version still grant admin rights without the header, and
   may store new admin grants via the permissions API: if admin mode must
   hold from the start, stop them before the new pods serve requests
   (e.g., scale the app to 0 replicas, then upgrade), or revoke stored
   admin grants once the rollout has finished.
5. **Owners' roles of groups:** the policy rule `owners-change-permissions`
   now covers `/ga4gh/registry/v1/services/:id/permissions/*` (before:
   `.../permissions/:user`), as user identifiers may contain slashes (with
   [several token issuers](configuration.md#several-token-issuers)), so it
   also covers the roles of groups (`.../permissions/groups/<group>`).
   Overlays that restrict owners' roles of groups (e.g.,
   `owners-change-group-permissions: null`) must now also change
   `owners-change-permissions`, or owners can grant and revoke roles of
   groups again: remove it, or redefine it for users only (see [Requiring
   login for reads](access-control.md#requiring-login-for-reads), also for
   the startup warning).

Noticeable, but no action needed:

- Startup checks of the group settings, of the admins and of the service
  info are FOCA's, with FOCA's log messages (other wording and loggers):
    - FOCA refuses group mappings without a group claim
      (`security.access_control.claim_groups`) and group claims with
      surrounding whitespace with a `ValueError` (before: a `RuntimeError`
      of the app), and doesn't warn if no group claim is set at all.
    - FOCA's admin check also counts configured rules
      (`security.access_control.policies`) that grant the role `admin`
      (only without admin mode, which refuses such rules), and
      its warning without admins no longer names what nobody can do in the
      mode of service types (see [Service
      types](configuration.md#service-types)).
    - FOCA warns of values of the service info in reserved example domains
      (e.g., `id`, `organization.url`, `contactUrl` and `documentationUrl`),
      and doesn't check `organization.name`.
- Requests rejected by the validation of the API specification still get
  the reason as `detail`, now from FOCA (`exceptions.validation_details`);
  the reasons are logged by FOCA. Responses that fail the validation of the
  API specification (e.g., of stored services that are invalid) are now
  logged by Connexion (`ERROR`, with the response), as FOCA only filters
  the log records of rejected requests.

## From 0.4.0

Data is kept. Before upgrading:

1. Check the environments of the stored services, e.g., with `mongosh` (see
   [MongoDB](deployment.md#mongodb) for access to the database of a Helm
   deployment):

    ```javascript
    db.getSiblingDB("serviceStore").services.distinct("environment")
    ```

    Registrations may only use the [allowed
    environments](configuration.md#service-environments) now, by default
    `prod`, `test`, `dev` and `staging`. To keep other values, allow them in
    `custom.endpoints.services.environments` in the same rollout; otherwise,
    services with other values are served, but can only be updated with an
    allowed value (see [Stored services](#stored-services)).
2. Add the rules `add-service-types` and `list-approvals` to overlays that
   give the policies as a list (see [Service types](#service-types)).
3. Tell clients that the registry sets the [timestamps](api.md#timestamps)
   now: those that users other than admins send are ignored.
4. Users other than admins may add at most 10 types that no service has
   (`custom.access_control.max_unused_service_types`; see [Service
   types](configuration.md#service-types)); change the limit in the same
   rollout if needed, and rate-limit `POST /services/types` and `POST
   /services` at the ingress (see [Rate limits](deployment.md#rate-limits)).
5. Overlays that set their own `x-bearerInfoFunc` (in
   `api.specs.registry.add_security_fields`) keep working, but tokens sent
   with a public read that the overlay's function rejects are then validated
   twice. The base config now
   uses `cloud_registry.ga4gh.registry.permissions.validate_bearer_token`, a
   thin wrapper around FOCA's `validate_token()`; drop the setting from the
   overlay, or point it to that function (see
   [Authentication](access-control.md#authentication)).

Then upgrade, and restart the app once after the rollout (or upgrade with
the app scaled to 0 replicas), so that it stores the types of the services
that pods of 0.4.0 registered during the rollout (see [Service
types](#service-types)). On startup, the app also creates its new indexes
and reconciles which services are pending; nothing needs to be done for
them. Switch to the mode `moderated` (see [Service
types](configuration.md#service-types)) only after that: while pods of 0.4.0
still register services, a user's pending type can hide another user's
service that a pod of 0.4.0 registered with the same type, until an admin
approves the type.

## From versions before 0.4.0

**Redeploy with a fresh install and an empty database; don't migrate the
data.** The previously deployed version (FOCA 0.12) accepted tokens of any
issuer and had no ownership checks, so anyone with any OpenID Connect token
could create, change or delete entries: its data can't be trusted. In the new
version, services stored before have no owner, so only admins could manage
them.

1. Optionally, export the registered services as a reference for their
   operators (`GET /ga4gh/registry/v1/services`).
2. Uninstall the release (`helm uninstall <release> -n <namespace>`; this
   deletes the MongoDB volume and data). Releases of the legacy charts can't
   be upgraded anyway: the chart renames its resources and changes the pod
   selectors.
3. Create a Secret with the MongoDB credentials (MongoDB requires
   authentication now; see [Authentication](deployment.md#authentication)).
4. Write the values file of the deployment (see
   [Configuration](deployment.md#configuration) of the Helm chart). The
   identity provider is deployment configuration now, without defaults (see
   [Deployment configuration](configuration.md#deployment-configuration)):
    - `hostName`: the public host name (required);
    - `config.security.auth.issuers`: the trusted token issuers, i.e., your
      identity provider (required; the base config trusts none);
    - `config.security.auth.audience`: the client IDs of the apps that call
      the registry, e.g., its front end (required);
    - `config.security.access_control.claim_groups`: the claim with the
      groups of users, also in `config.security.auth.userinfo_claims` if the
      identity provider releases it via user info only (required with group
      mappings);
    - `config.security.access_control.group_mapping`: the admin group mapped
      to the role `admin` (required, unless
      `config.security.access_control.require_admin` is `false`; before
      0.6.0, `config.custom.access_control.require_admin`);
    - `mongodb.auth.existingSecret`: the name of the Secret;
    - `config.service_info.service`: the service info of the deployment
      (e.g., `id`, `name`, `organization`, `contactUrl` and
      `environment: prod`; the defaults changed, see the configuration
      changes in the [release notes](releases/0.4.0.md#breaking-changes), so
      set the previous `id`, `ELIXIR_CLOUD_SERVICE_REGISTRY_1`, to keep it
      for clients), which is no longer set via the API;
    - remove `cloud_registry.appName` (use `fullnameOverride` to choose the
      resource names) and `apiServer`, and move any changes made to the
      `app-config` ConfigMap to `config`;
    - rename `host_name` to `hostName` and `cloud_registry` to
      `cloudRegistry` (rendering fails while the old keys are set), and
      `mongodb.volumeSize` to `mongodb.persistence.size` (rendering fails
      with `volumeSize`; see
      [Persistence](deployment.md#persistence));
    - optionally, enable the app's NetworkPolicy
      (`cloudRegistry.networkPolicy.enabled: true`), with
      `cloudRegistry.networkPolicy.ingressFrom` set to the pods of the
      cluster's ingress controller or Gateway (there are no default peers; see
      [Network policies](deployment.md#network-policies)). MongoDB's
      NetworkPolicy is enabled by default and only lets the app's pods
      connect (see [Network policies](deployment.md#network-policies)).
5. Install the chart attached to the release (`cloud-registry-{{ chart_version }}.tgz`):
   `helm install <release> cloud-registry-{{ chart_version }}.tgz -n <namespace> -f
   values-prod.yaml`. The chart version goes from 4.0.0 (the last chart
   version in the repository) back to the app version, which it now
   follows; that's no problem for a fresh install.
6. Ask the operators to register their services again (`POST /services`);
   they become their owners. Admins verify them (see [Verified
   services](api.md#verified-services)).

To keep the data anyway, dump and restore it as described in [From the legacy
charts](deployment.md#from-the-legacy-charts): the app then adds keys to the
stored services on startup and refuses to start while some of them have
invalid URLs or are duplicates (see [Stored services](#stored-services)), and
admins have to assign owners (`PUT /services/{serviceId}/permissions/{userId}`).

Docker Compose setups keep the data in a named volume now; see [Docker Compose
data](#docker-compose-data) to keep using the former bind mount.

## Docker Compose data

Earlier versions kept the data in `../data/cloud-registry/db`. To keep using
it, bind-mount it in a `docker-compose.override.yaml`:

```yaml
services:
    mongodb:
        volumes:
            - ../data/cloud-registry/db:/data/db
```

## Public URLs

Previously, `custom.endpoints.service.api_path` held the API base path itself
(e.g., `ga4gh/registry/v1`); remove that value when upgrading (see [Public
URLs](configuration.md#public-urls)).

## Service info

Previously, service info could be set via `POST /service-info` and was stored
in the database. That endpoint is gone, and stored service info is no longer
served (the `service_info` collection can be dropped): put it in the config
when upgrading (see [Service info](configuration.md#service-info)).

## Service types

Previously, service types were not stored: `GET /services/types` listed the
types of the registered services. Now they are stored (see [Service
types](api.md#service-types)). On startup, the app stores the types of the
registered services, so `GET /services/types` lists the same types as
before; afterwards, it also lists types that no service has (yet), and types
stay registered when the services that had them change or are deleted
(admins remove them). Types of stored services that aren't objects with the
strings `group`, `artifact` and `version` are skipped. Services registered by
pods of the previous version during a rolling update don't have their types
stored until the app restarts: restart it once after the rollout (e.g.,
`kubectl rollout restart deployment/<name> -n <namespace>`).

Overlays that give the policies (`security.access_control.policies`) as a
list must add the new rules `add-service-types` and `list-approvals` (see
`config.yaml`), or users can't add types with `POST /services/types` or list
[pending approvals](api.md#pending-approvals) with `GET /approvals`.

## Several token issuers

Trusting a second token issuer (see [Several token
issuers](configuration.md#several-token-issuers)) namespaces the
identifiers of users by issuer, also those of the issuer trusted so far,
unless its subjects are kept bare. Stored identifiers of its users (their
roles on services, roles granted via the permissions API, the service
types they added, the services they registered (for the [limit of services
per user](configuration.md#services-per-user)), their display names, and the
[invitations](access-control.md#invitations) they created or accepted) then
no longer match them (e.g., open invitations of owners are deleted when
they are accepted, as their creators seem to be no longer owners):

- **Recommended:** keep the subjects of the issuer trusted so far bare
  (`namespace: ""`), so that nothing needs to be migrated. Its users keep
  their identifiers, and those of the new issuer are namespaced.
- **Otherwise:** after the config change, namespace the stored identifiers
  of the issuer trusted so far with the migration of the image, with the
  app's new config (e.g., in a pod of the app, whose deployment name
  depends on the release; `--dry-run` only counts them). The migration,
  also with `--dry-run`, refuses to run while the config trusts a single
  issuer, or keeps the subjects of an issuer bare:

  ```bash
  kubectl exec deploy/cloud-registry -- \
    python -m cloud_registry.namespace_subjects https://login.example.org/oidc/
  ```

  The argument is the namespace of the issuer trusted so far (by default,
  the issuer). Its users lack their roles from the rollout until the
  migration, so run it right after the rollout, while users don't change
  roles or accept invitations (e.g., announce a short maintenance window):
  roles revoked during the migration may be restored by it, invitations
  accepted during it may be deleted or stay claimed although their role
  wasn't granted (create new ones then), and requests that store display
  names may fail (clients can retry them). Running app instances
  load the changed rules on their next request. Configured rules that name
  bare users (`security.access_control.policies`) are counted, but not
  changed: namespace them in the config. The migration can be run again
  (namespaced identifiers are kept).

## Stored services

Services stored before the registry set the [timestamps](api.md#timestamps)
(`createdAt`, `updatedAt`) keep those they were registered with, which
their owners could set to any value (before timestamps were checked, also
to values that aren't RFC 3339 date-times), until they are updated: then
`updatedAt` is set, and `createdAt` is kept if it is an RFC 3339 date-time
(services without one don't get one). Admins can correct them via the API
(`PUT /services/{serviceId}`); as their timestamps are checked, they send a
valid `createdAt`, or none, when they update services with invalid ones.

Services stored before the [environments were
checked](configuration.md#service-environments) may have other values (e.g.,
`production`); they are served as they are, but can only be updated with an
allowed value (or without `environment`). Allow their values in
`custom.endpoints.services.environments` to keep them.

Services stored before the [length limits](api.md#registrations) were
introduced may have longer values; they can't be updated unchanged, only
with the values shortened.

Services stored before organization fields other than `name` and `url` were
[rejected](api.md#registrations) may have further fields in `organization`,
of any size; they are still served, but can only be updated without them. The
app warns of them on startup, naming them. Have their owners (or admins)
update them via the API (`PUT /services/{serviceId}`), or remove the fields
in the database, e.g., with `mongosh` (see [MongoDB](deployment.md#mongodb)),
which keeps only `name` and `url`:

```javascript
db.getSiblingDB("serviceStore").services.updateOne(
  {id: "<id>", "organization.name": {$exists: true}, "organization.url": {$exists: true}},
  [
    {$set: {organization: {$arrayToObject: {$filter: {
      input: {$objectToArray: "$organization"},
      cond: {$in: ["$$this.k", ["name", "url"]]},
    }}}}},
  ],
)
```

Services whose organization lacks `name` or `url` (only data written outside
the API, which has always required both) aren't served: with response
validation on (`validate_responses` in the base config), `GET
/services/{serviceId}` returns `500 Internal Server Error` for them, and so
does the whole list, `GET /services`, until both are set. The app doesn't
warn of them on startup, and the example above doesn't match them
(`matchedCount: 0`), so that it doesn't leave an organization of `{}`. List
them, and set both fields (or delete the services), e.g.:

```javascript
db.getSiblingDB("serviceStore").services.find(
  {$or: [{"organization.name": {$exists: false}}, {"organization.url": {$exists: false}}]},
  {_id: 0, id: 1, organization: 1},
)
db.getSiblingDB("serviceStore").services.updateOne(
  {id: "<id>"},
  {$set: {"organization.name": "<name>", "organization.url": "<URL>"}},
)
```

Then clean up their further fields as above.

Services stored before the check of [duplicate
services](api.md#duplicate-services) get their key on startup; only services
without a key are checked again. If some of them have invalid URLs, or the same
URL and API type as another one, the app refuses to start and names all of
them. Invalid URLs include some that were accepted before: URLs with schemes
other than `http` and `https`, whitespace or control characters, malformed
percent-encodings (also in queries and fragments), characters that URLs only
allow percent-encoded (e.g., `<`, `|`, `\`), hosts with percent-encodings or
empty labels (e.g., `a..org`), IPv4 addresses not in dotted-decimal form (e.g.,
`127.1`), or port 0. Fix or delete
them in the database, e.g., with `mongosh` (see
[MongoDB](deployment.md#mongodb) for access to the database of a Helm
deployment):

```javascript
db.getSiblingDB("serviceStore").services.deleteOne({id: "<id>"})
db.getSiblingDB("serviceStore").services.updateOne({id: "<id>"}, {$set: {url: "<URL>"}})
```

Then start the app again; it adds the missing keys.

Services that already have a key (stored by `dev` images built before URLs
were checked, or before the stricter checks above) are not checked again: if
their URLs are now invalid, they are still served, but can only be updated
with a valid URL, and registrations of the same service with a validly spelled
URL may not be detected as duplicates. The app warns of them on startup,
naming them; update or delete them as above.

Services stored with `http` URLs (accepted before `https` became the only
[allowed scheme](configuration.md#service-url-schemes) by default) are still
served, but can only be updated with an allowed scheme; the app warns of them
on startup, naming them. Have their owners (or admins) update them with an
`https` URL via the API (`PUT /services/{serviceId}`), which also updates
their keys, or delete them; or allow `http` (not recommended, as clients
would send access tokens unencrypted).
