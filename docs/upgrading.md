# Upgrading

The [release notes](changelog.md) list the breaking changes of each release
and how to upgrade, e.g., [to 0.3.0](releases/0.3.0.md#upgrade). Upgrades of
Helm releases are described in [Deployment](deployment.md#upgrades). This page
describes how existing setups and data are affected.

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
percent-encodings (also in queries and fragments), or port 0. Fix or delete
them in the database, e.g., with `mongosh` (see
[MongoDB](deployment.md#mongodb) for access to the database of a Helm
deployment):

```javascript
db.getSiblingDB("serviceStore").services.deleteOne({id: "<id>"})
db.getSiblingDB("serviceStore").services.updateOne({id: "<id>"}, {$set: {url: "<URL>"}})
```

Then start the app again; it adds the missing keys.

Services that already have a key (stored by `dev` images built before URLs
were checked) are not checked on startup: if their URLs are now invalid, they
are still served, but can only be updated with a valid URL.

Services stored with `http` URLs (accepted before `https` became the only
[allowed scheme](configuration.md#service-url-schemes) by default) are still
served, but can only be updated with an allowed scheme; the app warns of them
on startup, naming them. Have their owners (or admins) update them with an
`https` URL via the API (`PUT /services/{serviceId}`), which also updates
their keys, or delete them; or allow `http` (not recommended, as clients
would send access tokens unencrypted).
