# Upgrading

The [release notes](changelog.md) list the breaking changes of each release,
e.g., [of 0.3.0](releases/0.3.0.md#breaking-changes), and link the upgrade
steps on this page. Upgrades of Helm releases are described in
[Deployment](deployment.md#upgrades). This page describes how to upgrade, and
how existing setups and data are affected.

## From versions before 0.3.0

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
      `config.custom.access_control.require_admin` is `false`);
    - `mongodb.auth.existingSecret`: the name of the Secret;
    - `config.service_info.service`: the service info of the deployment
      (e.g., `id`, `name`, `organization`, `contactUrl` and
      `environment: prod`; the defaults changed, see the configuration
      changes in the [release notes](releases/0.3.0.md#breaking-changes), so
      set the previous `id`, `ELIXIR_CLOUD_SERVICE_REGISTRY_1`, to keep it
      for clients), which is no longer set via the API;
    - remove `cloud_registry.appName` (use `fullnameOverride` to choose the
      resource names) and `apiServer`, and move any changes made to the
      `app-config` ConfigMap to `config`;
    - rename `host_name` to `hostName` and `cloud_registry` to
      `cloudRegistry` (rendering fails while the old keys are set);
    - optionally, enable the app's NetworkPolicy
      (`cloudRegistry.networkPolicy.enabled: true`), with
      `cloudRegistry.networkPolicy.ingressFrom` set to the pods of the
      cluster's ingress controller or Gateway (there are no default peers; see
      [Network policies](deployment.md#network-policies)). MongoDB's
      NetworkPolicy is enabled by default and only lets the app's pods
      connect (see [Network policies](deployment.md#network-policies)).
5. Install the chart attached to the release (`cloud-registry-0.3.0.tgz`):
   `helm install <release> cloud-registry-0.3.0.tgz -n <namespace> -f
   values-prod.yaml`. The chart version goes from 4.0.0 (the last chart
   version in the repository) back to 0.3.0, as it now follows the app
   version; that's no problem for a fresh install.
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

## Stored services

Services stored before timestamps were checked may have other values than
RFC 3339 date-times; they can't be updated unchanged, only with valid
timestamps (or without them). Services stored before the [length
limits](api.md#registrations) were introduced may have longer values; they
can't be updated unchanged, only with the values shortened.

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
