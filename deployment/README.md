# Helm chart

## Configuration

The image contains the app's complete base config
([`cloud_registry/config.yaml`](../cloud_registry/config.yaml)). The chart
renders a config overlay into the `<appName>-config` ConfigMap, which FOCA
merges into the base config on startup (via `FOCA_CONFIG_OVERLAY`):

- `host_name` sets the public URL of the app
  (`custom.endpoints.service`: `https://<host_name>:443`);
- `config` overrides keys of the base config; it takes precedence over the
  values generated from `host_name`. Mappings are merged recursively, all
  other values (including lists) replace those of the base config. Do not
  change `server.port` and `db.host`: the chart's Services expect port 8080
  and the `mongodb` host.

The overlay requires an image built with FOCA 0.14 or later (e.g.,
`elixircloud/cloud-registry:20261020` or later); older images ignore it.

Keep the values of each deployment in its own values file outside this
repository (e.g., `values-prod.yaml` in the deployment's infrastructure
repository), for example:

```yaml
host_name: registry.example.org
config:
  security:
    auth:
      # Client IDs of the apps calling the registry
      audience: ["my-portal-client-id"]
      verify_audience: True
```

Do not edit the ConfigMap with `kubectl edit`: Helm overwrites it on the next
upgrade. As `config` is stored in a ConfigMap, it must not contain secrets
(e.g., database credentials).

## Installation

Create the Secret with the MongoDB credentials (see
[Authentication](#authentication)) and set its name in the values file
(`mongodb.auth.existingSecret`), then install the chart from the
`deployment` folder:

```bash
helm install cloud-registry . -n <namespace> -f values-prod.yaml
```

`<namespace>` is the namespace to deploy to; `cloud-registry` is the name of
the release, which is referenced when upgrading or uninstalling.

## Upgrades

Change the values file (e.g., `cloud_registry.image` for a new image or
`config` for the config) and upgrade the release from the `deployment`
folder:

```bash
helm upgrade cloud-registry . -n <namespace> -f values-prod.yaml
```

The app pods are replaced when the config changes. `helm ls -n <namespace>`
lists the releases.

Upgrading from chart version 2 removes the `configmap-job-<appName>` Job, its
ServiceAccount and RoleBinding, and the `app-config` ConfigMap: move any
changes made to the config there to `config` in the values file first.

Chart version 2 used `mongo:noble`, which resolves to the latest MongoDB
version (e.g., 9.0), while chart version 3 defaults to `mongo:7.0`, which
cannot read the data of newer versions. **Before upgrading from chart version
2, check the running MongoDB version (see [Version](#version)) and set
`mongodb.image` to its major and minor version (e.g.,
`docker.io/library/mongo:9.0`).** Chart version 3 also enables MongoDB
authentication by default: see [Authentication](#authentication) for existing
databases.

## Probes

- **App:** ready once it serves its Swagger UI (`/ga4gh/registry/v1/ui/`;
  all API endpoints require authentication; set `cloud_registry.probePath`
  if the config disables the Swagger UI), checked every 10 seconds, so
  `kubectl rollout status deploy/<appName> -n <namespace>` waits until it can
  serve requests; up to 5 minutes are allowed for the start (startup probe);
  restarted after failing for a minute afterwards (liveness probe).
- **MongoDB:** ready once it accepts connections; up to 30 minutes are
  allowed for the start (startup probe), as MongoDB only listens on localhost
  while it initializes an empty data directory, and recovery after an
  unclean shutdown can take a while; restarted after failing for a minute
  afterwards (liveness probe).

## MongoDB

### Version

The chart pins MongoDB (`mongodb.image`, default `mongo:7.0`; MongoDB 8.0
refuses to start on Linux kernels 6.19 to 7.0.13, cf.
[SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). MongoDB
only starts on data whose feature compatibility version (FCV) it supports,
so check the running version and the FCV before changing the image of an
existing database. With authentication (the default), the command uses the
root user's credentials from the container's environment:

```bash
kubectl exec deploy/mongodb -n <namespace> -- sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin --eval "printjson({version: db.version(), fcv: db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1}).featureCompatibilityVersion})"'
```

Without authentication (e.g., chart version 2), omit `-u`, `-p` and
`--authenticationDatabase`.

- **Upgrades** go one major version at a time (e.g., 6.0 → 7.0 → 8.0): set
  the image of the next version, wait for the rollout, then set the FCV to
  that version (e.g.,
  `db.adminCommand({setFeatureCompatibilityVersion: "7.0", confirm: true})`)
  before the next step.
- **Downgrades** of the MongoDB binary are not supported for MongoDB
  Community Edition (which the `mongo` images contain). Keep the running
  version instead, or dump the database (`mongodump`) and restore it
  (`mongorestore`) into a new, empty volume with the older version.

The MongoDB Deployment uses the `Recreate` strategy, as the data volume can
only be used by one pod: changing the image stops MongoDB, and an image that
cannot read the data leaves it down until the image is changed back. See the
MongoDB release notes for details.

### Authentication

MongoDB requires authentication by default. Before installing the chart,
create a Secret with the credentials of the MongoDB root user and the app
user:

```bash
kubectl create secret generic cloud-registry-mongodb -n <namespace> \
  --from-literal=root-username=root \
  --from-literal=root-password="$(openssl rand -hex 24)" \
  --from-literal=username=cloud-registry \
  --from-literal=password="$(openssl rand -hex 24)"
```

and set its name in the values file:

```yaml
mongodb:
  auth:
    existingSecret: cloud-registry-mongodb
```

Without a Secret name, rendering the chart fails. To run MongoDB without
authentication, set `mongodb.auth.enabled: false` explicitly; any pod that
can reach the MongoDB service (it is only exposed within the cluster) can
then read and modify the data.

MongoDB creates both users when it starts with an empty data directory (the
app user may only access the app's database, `serviceStore`, which is also
set in the init script, `templates/mongo-init-script.yaml`); the app connects
as the app user. Credentials never appear in values files or
ConfigMaps.

Commands that need privileges, e.g., to check the version, can use the root
user's credentials from the container's environment:

```bash
kubectl exec deploy/mongodb -n <namespace> -- sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin --eval "db.version()"'
```

Changing the Secret does not change the users: to change a password, first
change it in MongoDB (e.g.,
`db.getSiblingDB("serviceStore").changeUserPassword("cloud-registry", "<new password>")`),
then update the Secret and restart the app (`kubectl rollout restart
deploy/<appName> -n <namespace>`).

For an existing database without authentication (e.g., from chart version
2), create the users before enabling authentication, with the credentials
from the Secret:

```bash
kubectl exec -it deploy/mongodb -n <namespace> -- mongosh
```

```javascript
db.getSiblingDB("admin").createUser({user: "root", pwd: "<root-password>", roles: ["root"]})
db.getSiblingDB("serviceStore").createUser({user: "cloud-registry", pwd: "<password>", roles: [{role: "readWrite", db: "serviceStore"}]})
```

If the root user exists but the app user is missing (e.g., because the first
start was interrupted), log in as the root user and create only the app user:

```bash
kubectl exec -it deploy/mongodb -n <namespace> -- sh -c 'mongosh \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin'
```

```javascript
db.getSiblingDB("serviceStore").createUser({user: "cloud-registry", pwd: "<password>", roles: [{role: "readWrite", db: "serviceStore"}]})
```
