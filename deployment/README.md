# Helm chart

The chart deploys the app and a MongoDB database on Kubernetes (and
OpenShift). It is a thin chart over FOCA's library chart `foca-common` (the
version in `Chart.yaml`), which renders all resources from the values
described here and in `values.yaml` (see the [library chart's
documentation](https://github.com/elixir-cloud-aai/foca/blob/chart-v0.1.0/docs/helm-chart.md)).
The chart attached to a release includes the library chart; the chart in the
repository downloads it with `helm dependency build` (see
[Installation](#installation)).

## Configuration

The image contains the app's complete base config
([`cloud_registry/config.yaml`](https://github.com/elixir-cloud-aai/cloud-registry/blob/dev/cloud_registry/config.yaml)). The chart
renders a config overlay into the `<app>-config` ConfigMap (see
[Names and labels](#names-and-labels)), which FOCA merges into the base
config on startup (via `FOCA_CONFIG_OVERLAY`):

- `hostName` sets the public URL of the app
  (`custom.endpoints.service`: `https://<hostName>:443`);
- `db.host` is set to the release's MongoDB Service (`<app>-mongodb`),
  unless MongoDB is external (see [External MongoDB](#external-mongodb));
- `config` overrides keys of the base config; it takes precedence over the
  generated values. Mappings are merged recursively, all other values
  (including lists) replace those of the base config. Do not change
  `db.host` (unless the app is to use another MongoDB); `server.host` and
  `server.port` have no effect, as they apply to the development server only
  (see [Server](#server)).

The chart supports only images built with FOCA 0.15 or later: any release
from 0.3.0 on (the first one tagged from Git; see its [release
notes](https://cloud-registry.readthedocs.io/en/latest/releases/0.3.0/)), or `dev` and `sha-<commit>` images of
commits since the upgrade to FOCA 0.15. Older images ignore the overlay or
the service info (`config.service_info`), and the chart rejects their way of
setting it (`config.custom.endpoints.service_info`).

Each deployment must set the audience (`config.security.auth.audience`;
rendering fails without it, as the app would refuse to start, unless
`config.security.auth.verify_audience` is `false`, which accepts tokens for
any audience) and map an admin group to the role `admin`
(`config.security.access_control.group_mapping`; rendering fails without one,
unless `config.custom.access_control.require_admin` is `false`). Keep the
values of each deployment in its own values file outside this repository
(e.g., `values-prod.yaml` in the deployment's infrastructure repository), for
example:

```yaml
hostName: registry.example.org
config:
  security:
    auth:
      # Client IDs of the apps calling the registry
      audience: ["my-portal-client-id"]
    access_control:
      # Members of this group may manage all services
      group_mapping:
        "urn:geant:lifescience-ri.eu:group:elixir:ECP_CLN:ADMIN#aai.lifescience-ri.eu": admin
```

Users removed from a group keep its roles (also `admin`) for up to
`config.security.auth.cache_ttl` seconds (default: 300), as FOCA caches the
user info with the group memberships per token; set
`config.security.auth.validate_userinfo: true` or a lower `cache_ttl` to
shorten the delay (see [Group membership
changes](https://cloud-registry.readthedocs.io/en/latest/access-control/#group-membership-changes)).

The service info served at `/service-info` is set via
`config.service_info.service` (mappings are merged, so set only the fields to
change, e.g., `environment: prod`, and set optional fields of the default to
`null` to remove them). The defaults describe the app, with placeholders for
`id` and `organization` and no `contactUrl`, so set all fields that describe
the deployment: `id`, `name`, `description`, `organization` (both `name` and
`url`), `contactUrl` and `environment` (the default is `dev`; see the example
in `values.yaml`). The app logs a warning on startup while `id` or
`organization` are the placeholders. Pods are restarted when the ConfigMap
changes, and the app refuses to start if the service info is invalid.

Service URLs must be `https` URLs by default, as clients send access tokens
to the registered services; `config.custom.endpoints.services.url_schemes`
(e.g., `[http, https]`) allows `http` too, which is not recommended (see
[Service URL
schemes](https://cloud-registry.readthedocs.io/en/latest/configuration/#service-url-schemes)).

Do not edit the ConfigMap with `kubectl edit`: Helm overwrites it on the next
upgrade. As `config` is stored in a ConfigMap, it must not contain secrets
(e.g., database credentials).

## Image

The app image is `cloudRegistry.image` (default
`elixircloud/cloud-registry`) with tag `cloudRegistry.tag`, which defaults to
the chart's `appVersion`. Charts attached to [GitHub
releases](https://github.com/elixir-cloud-aai/cloud-registry/releases) have
the release version as `version` and `appVersion`, so they deploy the image of
their release (`X.Y.Z`). In the repository, both are placeholders, and
`appVersion` is `dev`: the chart deploys the latest image of branch `dev`,
which changes with every commit. Deployments from the repository must set the
tag of a release (e.g., `0.3.0`) or of a commit on `dev` (`sha-<commit>`, the
full commit SHA) in the values file:

```yaml
cloudRegistry:
  tag: sha-0123456789abcdef0123456789abcdef01234567
```

`cloudRegistry.image` may also include a tag or digest (e.g.,
`elixircloud/cloud-registry:0.3.0`, as in earlier chart versions); then
`cloudRegistry.tag` must be empty. See [Versioning and
releases](https://cloud-registry.readthedocs.io/en/latest/development/#versioning-and-releases) for the image tags.

The image is pulled on every start of a pod
(`cloudRegistry.imagePullPolicy: Always`), so that moving tags such as `dev`
deploy the latest image; set `IfNotPresent` for fixed tags or digests, or
`Never` for images loaded into the nodes (e.g., with `kind load`). MongoDB's
image is pulled the same way (`mongodb.imagePullPolicy`), so that restarts get
the latest patch version of the pinned minor version (see [Version](#version)).

## Installation

Create the Secret with the MongoDB credentials (see
[Authentication](#authentication)) and set its name in the values file
(`mongodb.auth.existingSecret`), then install the chart of a
[release](https://github.com/elixir-cloud-aai/cloud-registry/releases)
(`cloud-registry-X.Y.Z.tgz`, attached to the release `vX.Y.Z`):

```bash
helm install cloud-registry \
  https://github.com/elixir-cloud-aai/cloud-registry/releases/download/vX.Y.Z/cloud-registry-X.Y.Z.tgz \
  -n <namespace> -f values-prod.yaml
```

To install the chart from the `deployment` folder of the repository instead
(e.g., to test changes of the chart), download the library chart (into
`charts/`, the version locked in `Chart.lock`), and set `cloudRegistry.tag`
(see [Image](#image)), as the chart deploys the moving `dev` image otherwise:

```bash
helm dependency build .
helm install cloud-registry . -n <namespace> -f values-prod.yaml
```

`<namespace>` is the namespace to deploy to; `cloud-registry` is the name of
the release, which is referenced when upgrading or uninstalling. Helm prints
the app's URL and a test request after installing (`helm get notes
cloud-registry -n <namespace>` shows them again).

## Names and labels

Resource names are derived from the release name, so that several releases
can be installed in one namespace:

- **App** (Deployment, Service, Ingress or Route, NetworkPolicy; ConfigMap
  `<app>-config`): `<app>` is `<release>-cloud-registry`, or the release
  name if it contains `cloud-registry` (e.g., `cloud-registry` for the
  release `cloud-registry`, `prod-cloud-registry` for the release `prod`);
- **MongoDB** (Deployment, Service, PersistentVolumeClaim, NetworkPolicy;
  ConfigMap `<app>-mongodb-init-script`): `<app>-mongodb`; the app connects
  to this Service.

`nameOverride` replaces `cloud-registry` in the names and the
`app.kubernetes.io/name` label, `fullnameOverride` replaces `<app>`
(camelCase, as in other Helm charts). Set them when installing only:
changing them later changes the pod selectors of the Deployments, which
cannot be changed, so the upgrade fails. Names are truncated to 63
characters. The examples in this document use the release `cloud-registry`.

All resources have the
[standard labels](https://helm.sh/docs/chart_best_practices/labels/)
(`app.kubernetes.io/name`, `instance`, `version` and `managed-by`, and
`helm.sh/chart`); pods are selected by name, instance and
`app.kubernetes.io/component` (`app` or `mongodb`), e.g.:

```bash
kubectl get pods -n <namespace> -l app.kubernetes.io/instance=cloud-registry
```

## Ingress

By default, the chart creates an Ingress for `hostName`
(`cloudRegistry.ingress`) that routes all paths (`/`) to the app, including
FOCA's permissions API (`/admin/access-control/`, for admins only; see
[Permissions API](https://cloud-registry.readthedocs.io/en/latest/operations/#permissions-api)). To keep that API off
the internet, block `/admin/` in the proxy in front of the cluster, or
disable the chart's Ingress (`enabled: false`) and create one of your own
that routes only `/ga4gh/registry/v1/` to the app's Service (port `http`).
On OpenShift, the chart then creates a Route for all paths instead (see
[OpenShift Route](#openshift-route)), so there, block `/admin/` in front of
the router. The Ingress is configured with:

- `className` sets the Ingress class (default: the cluster's default class);
- `annotations` sets annotations, e.g., for the ingress controller; they
  take precedence over those set by the chart (see below), and `null`
  removes one of them;
- `https.enabled` requests a certificate from cert-manager (via the
  ClusterIssuer `https.issuer`, by annotations that `annotations` can
  override) and serves HTTPS with it.

The app rejects request bodies over `config.server.max_content_length` bytes
(default: 1 MiB; see [Request size
limit](https://cloud-registry.readthedocs.io/en/latest/configuration/#request-size-limit)). The Ingress sets the
same limit for ingress-nginx, in bytes
(`nginx.ingress.kubernetes.io/proxy-body-size`), so that larger requests are
rejected before they reach the app; to raise the limit, e.g., to 8 MiB, set
only the app's:

```yaml
config:
  server:
    max_content_length: 8388608
```

Set `cloudRegistry.ingress.annotations` to give ingress-nginx another limit,
or set the annotation to `null` to remove it. Other ingress controllers, and
OpenShift Routes, ignore the annotation; check their documentation for how to
limit request bodies. Without such a limit, only the app's applies.

### TLS

The app advertises HTTPS URLs on port 443 (e.g., in `Location` headers; see
[Configuration](#configuration)), but by default, the Ingress has no TLS
(`https.enabled: false`) and serves plain HTTP, so TLS must be terminated in
front of the cluster (e.g., by a load balancer); Helm's notes after installing
say so. Otherwise, set `https.enabled: true` to serve HTTPS with a certificate
from cert-manager; ingress-nginx then redirects HTTP requests to HTTPS by
default.

If TLS is terminated in front of the cluster, ingress-nginx can't tell HTTP
from HTTPS requests by itself, so it doesn't redirect them. Optionally, have
it redirect requests that the proxy in front marks as HTTP
(`X-Forwarded-Proto`), if the controller trusts that header
(`use-forwarded-headers` in its ConfigMap; otherwise, all requests are
redirected, in a loop):

```yaml
cloudRegistry:
  ingress:
    annotations:
      nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
```

Better yet, let the proxy in front redirect HTTP requests itself.

### Rate limits

The app doesn't limit how often or how many services a user registers. Limit
the request rate at the ingress instead, e.g., per client IP address with
ingress-nginx (requests per second, a burst of 5 times as many, and
concurrent connections; excess requests get `503`, or the status set by the
controller's `limit-req-status-code`):

```yaml
cloudRegistry:
  ingress:
    annotations:
      nginx.ingress.kubernetes.io/limit-rps: "10"
      nginx.ingress.kubernetes.io/limit-burst-multiplier: "5"
      nginx.ingress.kubernetes.io/limit-connections: "20"
```

Behind a proxy or load balancer, the controller must see the clients'
addresses (e.g., `use-forwarded-headers` or the PROXY protocol), otherwise all
requests count for the proxy. For OpenShift Routes, see the
`haproxy.router.openshift.io/rate-limit-connections` annotations.

### OpenShift Route

With `enabled: false`, the chart creates an OpenShift Route for all paths,
with TLS termination at the router, instead, if the cluster serves Routes
(`route.openshift.io/v1`); otherwise the app is only reachable within the
cluster, and Helm prints a warning after installing.

## Security context

Both containers run as non-root users, without privilege escalation or
capabilities, with the runtime's default seccomp profile and a read-only root
file system; they write to `emptyDir` volumes (`/tmp`; for MongoDB also
`/data/configdb`) and MongoDB to its data volume. The app image runs as user
1000; the MongoDB image starts as root and switches to user `mongodb` (999),
so the chart sets that user and group (`runAsUser`, `runAsGroup`), and
`fsGroup` makes the data volume writable for it (unless the storage ignores
`fsGroup`).

Neither pod gets a service account token (`automountServiceAccountToken:
false`), as neither calls the Kubernetes API.

The `/tmp` volumes are limited to 64 MiB each by `tmpSizeLimit` of
`cloudRegistry` and `mongodb` (a positive Kubernetes quantity, e.g., `64Mi`,
or a number of bytes; `null` removes the limit); the kubelet evicts pods that
exceed it.

On OpenShift (detected by the API `security.openshift.io/v1`), the chart
omits `runAsUser`, `runAsGroup` and `fsGroup`, as OpenShift assigns them from
the namespace's range and its default security context constraints reject
other IDs.

The contexts are set in the values (`podSecurityContext` and
`securityContext` of `cloudRegistry` and `mongodb`); mappings are merged
with the defaults, and `null` removes a default. Outside OpenShift, MongoDB
needs `runAsUser`: without it, the image starts as root, which
`runAsNonRoot` rejects (the pod fails with `CreateContainerConfigError`).
To run it as another user, change the IDs together, e.g.:

```yaml
mongodb:
  podSecurityContext:
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
```

If the storage manages the permissions of the volume itself, `fsGroup: null`
removes `fsGroup`.

## Network policies

The chart creates
[NetworkPolicies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
that restrict which pods may connect to its pods (ingress); connections from
the pods, e.g., of the app to MongoDB and to the token issuers (OpenID
Connect discovery, JWKS), are not restricted. They only take effect if the
cluster's network plugin enforces NetworkPolicies (e.g., Calico or Cilium);
other clusters accept and ignore them. NetworkPolicies add up: a pod that
several policies select accepts the connections that any of them allows, so
check other policies that select the chart's pods (e.g., namespace-wide
allow rules) before relying on these restrictions.

- **MongoDB** (`mongodb.networkPolicy.enabled`, default `true`): only the
  app's pods of the same release may connect, on MongoDB's port (selected by
  the labels of [Names and labels](#names-and-labels)); other pods, e.g., of
  other releases in the namespace, can't. `kubectl exec`, as in the
  commands of [MongoDB](#mongodb), doesn't go through the pod network and
  is not affected; to connect from other pods (e.g., a backup job), disable
  the policy, or add a NetworkPolicy that allows them (NetworkPolicies add
  up).
- **App** (`cloudRegistry.networkPolicy.enabled`, default `false`): only the
  NetworkPolicy peers in `cloudRegistry.networkPolicy.ingressFrom` may
  connect, on the app's port. The default allows the namespace
  `ingress-nginx`, where ingress-nginx usually runs, by its label
  `kubernetes.io/metadata.name` (set on all namespaces from Kubernetes 1.21;
  label it on older clusters), so before enabling the policy, check that it
  matches the cluster's ingress controller (or the OpenShift router);
  otherwise, the app is unreachable through the Ingress (or Route). Add any
  other clients in the cluster that call the app's Service directly, e.g.:

  ```yaml
  cloudRegistry:
    networkPolicy:
      enabled: true
      ingressFrom:
      # The pods of ingress-nginx in the namespace `ingress-nginx` (stricter
      # than the default, which allows all pods of that namespace)
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: ingress-nginx
        podSelector:
          matchLabels:
            app.kubernetes.io/name: ingress-nginx
      # A client in the release's namespace
      - podSelector:
          matchLabels:
            app.kubernetes.io/name: my-portal
      # On OpenShift: the router's namespace
      # - namespaceSelector:
      #     matchLabels:
      #       policy-group.network.openshift.io/ingress: ""
  ```

  `ingressFrom` replaces the default; rendering fails if it is empty (which
  would allow all sources), and the schema rejects unknown peer fields and an
  `ipBlock` without `cidr` or combined with selectors. An ingress controller
  in the host network (e.g., `hostNetwork: true`) connects from the nodes'
  addresses, which namespace selectors don't match; allow them with an
  `ipBlock` instead.

Neither policy blocks the probes: connections from the pod's own node, where
the kubelet runs them, are always allowed (see "The two sorts of pod
isolation" in the Kubernetes documentation).

## Server

The image serves the app with Gunicorn on port 8080 (see
[Server](https://cloud-registry.readthedocs.io/en/latest/configuration/#server) in the app's configuration). The
chart passes the settings in `cloudRegistry.gunicorn` to it (via
`GUNICORN_CMD_ARGS`); the defaults are the image's:

```yaml
cloudRegistry:
  gunicorn:
    workers: 1          # worker processes
    threads: 4          # threads per worker, i.e., requests served at once
    timeout: 30         # seconds until workers whose main loop hangs restart
    gracefulTimeout: 20 # seconds to finish requests on shutdown
    keepalive: 75       # seconds until idle keep-alive connections are closed
    extraArgs: []       # further options, e.g., ["--access-logfile", "-"]
```

Rendering fails if `extraArgs` set `--bind`/`-b` (the chart's Service,
container port and probes use port 8080), `--preload` (each worker must
create its own MongoDB client after forking) or `--graceful-timeout` (set
`gracefulTimeout`, from which the termination grace period is derived), also
abbreviated or as `--option=value`.

Each worker loads the whole app (about 55 MiB of memory; the default memory
limit, 128 MiB, fits one), so raise `cloudRegistry.resources` before
raising `workers`, or raise `threads` instead. Each worker runs the app's
startup checks; if they fail, Gunicorn exits and the container restarts
(see the logs: `kubectl logs deploy/<app> -n <namespace> --previous`).

`timeout` restarts workers whose main loop stops responding; requests run in
threads, so slow or stuck requests don't count. Keep `keepalive` longer than
the ingress controller's keep-alive timeout for upstream connections (60
seconds for ingress-nginx, `upstream-keepalive-timeout`): otherwise, it may
reuse a connection just as Gunicorn closes it, and the request fails with
`502` (non-idempotent requests, e.g., `POST`, are not retried). Idle
connections wait in the worker's poller, not in its threads.

On SIGTERM, Gunicorn stops accepting connections at once, while the ingress
controller and the Service may still send requests to the pod for a few
seconds. So the app keeps serving for `cloudRegistry.preStopSleepSeconds`
(default 5; `0` removes the `preStop` hook) before it shuts down, then
workers get `gracefulTimeout` seconds to finish their requests. The pod's
termination grace period is the sum of both plus 10 seconds. While the
ingress controller holds idle connections to the pod, shutdown usually takes
the full `gracefulTimeout`, so raising it slows down every rollout.

With the read-only root file system, Gunicorn keeps the heartbeat files of
its workers in `/dev/shm` (or in the `/tmp` volume if `/dev/shm` isn't
writable). Gunicorn's threads read requests themselves, so serve the app
through a proxy that buffers requests (ingress-nginx does by default), so
that slow clients can't tie them up. OpenShift Routes (HAProxy) don't buffer
request bodies by default, so there, slow uploads hold Gunicorn threads until
they complete.

## Upgrades

Change the values file (e.g., `config` for the config) and upgrade the
release with the chart of the new release, or with the `deployment` folder
and `cloudRegistry.tag` set to the new image (see
[Installation](#installation)). The chart of a release deploys its image only
if `cloudRegistry.tag` is empty and `cloudRegistry.image` has no tag (see
[Image](#image)), so remove a tag set for an earlier install from the values
file:

```bash
helm upgrade cloud-registry \
  https://github.com/elixir-cloud-aai/cloud-registry/releases/download/vX.Y.Z/cloud-registry-X.Y.Z.tgz \
  -n <namespace> -f values-prod.yaml
```

The app pods are replaced when the config changes. `helm ls -n <namespace>`
lists the releases.

### From the legacy charts

The legacy charts (versions 2.x and 3.0.0, installed from the repository
before charts were released from Git tags) used other resource names and
selectors. The current chart (released from Git tags, 0.3.0 and later, or
installed from the repository) renames the resources (see
[Names and labels](#names-and-labels); e.g., `mongodb` becomes
`cloud-registry-mongodb`) and changes the selectors of the Deployments,
which cannot be changed in place, so **releases of the legacy charts cannot
be upgraded: uninstall them and install the current chart.** An upgrade
would delete the `mongodb` PersistentVolumeClaim, so `helm upgrade` fails
for such releases (only when Helm can query the cluster: not in
`helm template` or client-side dry runs). Uninstalling also deletes the
claim and, usually, the data with it.

To keep the data, dump it before uninstalling and restore it into the new
release. The app is down from the first to the last step. The commands are
for the release `cloud-registry` (in chart versions 2 and 3, the app
Deployment is called after `cloud_registry.appName`, by default
`cloud-registry`, and the MongoDB Deployment `mongodb`):

1. Stop the app and wait until its pods are gone, so that the data does
   not change during the dump (the pods of chart versions 2 and 3 have the
   label `app=<appName>`):

   ```bash
   kubectl scale deploy/cloud-registry -n <namespace> --replicas=0
   kubectl wait pod -l app=cloud-registry -n <namespace> --for=delete \
     --timeout=120s
   ```

2. Dump the app's database to a local file, and check that MongoDB can read
   the dump (a dry run of the restore); continue only if `Dump OK` is
   printed:

   ```bash
   kubectl exec deploy/mongodb -n <namespace> -- sh -c 'mongodump --quiet \
     -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
     --authenticationDatabase admin --db serviceStore --archive' \
     > serviceStore.archive \
   && kubectl exec -i deploy/mongodb -n <namespace> -- sh -c 'mongorestore \
     --dryRun -u "$MONGO_INITDB_ROOT_USERNAME" \
     -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin \
     --archive' < serviceStore.archive \
   && echo "Dump OK"
   ```

   Without authentication (chart version 2, or `mongodb.auth.enabled:
   false`), omit `-u`, `-p` and `--authenticationDatabase` (here and in step
   7).

3. Optionally, keep the old volume as a fallback: set the reclaim policy of
   its PersistentVolume to `Retain`, so that it is kept (with the data) when
   the claim is deleted:

   ```bash
   kubectl patch pv "$(kubectl get pvc mongodb -n <namespace> \
     -o jsonpath='{.spec.volumeName}')" \
     -p '{"spec": {"persistentVolumeReclaimPolicy": "Retain"}}'
   ```

   (Annotating the claim with `helm.sh/resource-policy=keep` does not keep
   it: Helm reads that annotation from the chart's manifests, not from the
   cluster.)

4. Uninstall the release:

   ```bash
   helm uninstall cloud-registry -n <namespace>
   ```

5. Update the values file: remove `cloud_registry.appName` (rendering fails
   while it is set; `fullnameOverride` sets the names instead), and rename
   `host_name` to `hostName` and `cloud_registry` to `cloudRegistry`
   (camelCase, following Helm's conventions; rendering fails while the old
   keys are set). From chart version 2, move any changes made to the
   `app-config` ConfigMap to `config`, and create the Secret with the MongoDB
   credentials (see [Authentication](#authentication)); from chart version 3,
   keep `mongodb.auth.existingSecret`. MongoDB creates the users from the
   Secret on its first start with the new, empty volume. Dumps of newer
   MongoDB versions (e.g., `mongo:noble` of chart version 2) may not restore
   into the chart's default version (see [Version](#version)); set
   `mongodb.image` to the dumped version if needed.

6. Install the current chart (see [Installation](#installation)), stop its
   app again, so that it does not write during the restore, and wait for
   MongoDB:

   ```bash
   kubectl scale deploy/cloud-registry -n <namespace> --replicas=0
   kubectl wait pod -l app.kubernetes.io/instance=cloud-registry,app.kubernetes.io/component=app \
     -n <namespace> --for=delete --timeout=120s
   kubectl rollout status deploy/cloud-registry-mongodb -n <namespace>
   ```

7. Restore the dump, replacing the collections the app has created, then
   start the app:

   ```bash
   kubectl exec -i deploy/cloud-registry-mongodb -n <namespace> -- sh -c \
     'mongorestore --quiet --drop --archive \
     -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
     --authenticationDatabase admin' < serviceStore.archive
   kubectl scale deploy/cloud-registry -n <namespace> --replicas=1
   ```

Once the new release works, delete a retained volume
(`kubectl delete pv <name>`) and the local dump.

### To app version 0.3.0

Since app version 0.3.0, services are unique by URL and API type. For
releases installed with the current chart (from the repository) that run an
older image, upgrade with the app scaled to 0 replicas, so that no pod of the
old version registers services while the new one adds keys to the stored
ones:

```bash
kubectl scale deploy/cloud-registry -n <namespace> --replicas=0
helm upgrade cloud-registry \
  https://github.com/elixir-cloud-aai/cloud-registry/releases/download/v0.3.0/cloud-registry-0.3.0.tgz \
  -n <namespace> -f values-prod.yaml
```

(Releases of the legacy charts are uninstalled and reinstalled instead, see
above; after restoring their data, the new version adds the keys on its first
start.) If stored services without a key have invalid URLs or the same URL
and API type, the new version refuses to start and names them: fix or delete
them in MongoDB (see [Stored services](https://cloud-registry.readthedocs.io/en/latest/upgrading/#stored-services)
and [Authentication](#authentication) for credentials). See the [release
notes](https://cloud-registry.readthedocs.io/en/latest/releases/0.3.0/) for all breaking changes;
they recommend a fresh install with an empty database for deployments of the
previous version (FOCA 0.12).

The values keys `host_name` and `cloud_registry` are renamed to `hostName`
and `cloudRegistry` (rendering fails while the old keys are set): rename
them in the values file, and upgrade with it (`-f`), not with
`--reuse-values`, which would bring back the old keys stored in the release
(use `--reset-values` if needed).

## Probes

- **App:** ready once it serves its Swagger UI (`/ga4gh/registry/v1/ui/`;
  it needs neither authentication nor the database; set
  `cloudRegistry.probePath` if the config disables the Swagger UI), checked
  every 10 seconds, so `kubectl rollout status deploy/cloud-registry -n
  <namespace>` waits until it can serve requests; up to 5 minutes are
  allowed for the start (startup probe); restarted after failing for a
  minute afterwards (liveness probe). Probes are served by the app's
  Gunicorn threads like other requests (with a timeout of 5 seconds), so
  they fail if all threads stay busy, e.g., with slow requests.
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
kubectl exec deploy/cloud-registry-mongodb -n <namespace> -- sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin --eval "printjson({version: db.version(), fcv: db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1}).featureCompatibilityVersion})"'
```

Without authentication (e.g., `mongodb.auth.enabled: false`, or chart version
2, where the Deployment is called `mongodb`), omit `-u`, `-p` and
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
kubectl create secret generic cloud-registry-mongodb-credentials -n <namespace> \
  --from-literal=root-username=root \
  --from-literal=root-password="$(openssl rand -hex 24)" \
  --from-literal=username=cloud-registry \
  --from-literal=password="$(openssl rand -hex 24)"
```

and set its name in the values file:

```yaml
mongodb:
  auth:
    existingSecret: cloud-registry-mongodb-credentials
```

Without a Secret name, rendering the chart fails. To run MongoDB without
authentication, set `mongodb.auth.enabled: false` explicitly; any pod that
can reach the MongoDB service (it is only exposed within the cluster) can
then read and modify the data.

MongoDB creates both users when it starts with an empty data directory, with
an init script (ConfigMap `<app>-mongodb-init-script`); the app connects as
the app user. The app user may only access the app's database,
`serviceStore`, which also holds the rules of access control
(`security.access_control.db_name`); if the config sets another database for
them (`config.security.access_control.db_name`), the app user gets access to
it too. As the init script only runs for an empty data directory, grant the
app user (`username` in the Secret, e.g., `cloud-registry`) the `readWrite`
role on that database yourself when changing it for an installed release:

```bash
kubectl exec deploy/cloud-registry-mongodb -n <namespace> -- sh -c 'mongosh --quiet \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin --eval "db.getSiblingDB(\"serviceStore\").grantRolesToUser(\"<app user>\", [{role: \"readWrite\", db: \"<database>\"}])"'
```

Credentials never appear in values files or ConfigMaps.

Commands that need privileges can use the root user's credentials from the
container's environment, as in [Version](#version).

Changing the Secret does not change the users: to change a password, first
change it in MongoDB (e.g.,
`db.getSiblingDB("serviceStore").changeUserPassword("cloud-registry", "<new password>")`),
then update the Secret and restart the app (`kubectl rollout restart
deploy/cloud-registry -n <namespace>`). Until the app has restarted, its
requests to the database fail, so change passwords when a short outage is
acceptable. Helm does not restart pods when the Secret changes (only when the
config changes), as the chart does not manage the Secret.

For an existing database without authentication (deployed with
`mongodb.auth.enabled: false`), MongoDB enforces authentication as soon as
the chart enables it, but does not create the users. Create them first, while authentication is still
off:

1. Create the Secret (see above).
2. In the running MongoDB, create the users with the credentials from the
   Secret:

   ```bash
   kubectl exec -it deploy/cloud-registry-mongodb -n <namespace> -- mongosh
   ```

   ```javascript
   db.getSiblingDB("admin").createUser({user: "root", pwd: "<root-password>", roles: ["root"]})
   db.getSiblingDB("serviceStore").createUser({user: "cloud-registry", pwd: "<password>", roles: [{role: "readWrite", db: "serviceStore"}]})
   ```

3. Upgrade the release with authentication enabled and
   `mongodb.auth.existingSecret` set.

If the root user exists but the app user is missing (e.g., because the first
start was interrupted), log in as the root user and create only the app user:

```bash
kubectl exec -it deploy/cloud-registry-mongodb -n <namespace> -- sh -c 'mongosh \
  -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin'
```

```javascript
db.getSiblingDB("serviceStore").createUser({user: "cloud-registry", pwd: "<password>", roles: [{role: "readWrite", db: "serviceStore"}]})
```

### External MongoDB

To use a MongoDB outside the release (e.g., a managed database), set
`mongodb.enabled: false` and its host (and port) in the config; the chart then
deploys no MongoDB (no Deployment, Service, volume, init script or
NetworkPolicy), and rendering fails without `config.db.host`:

```yaml
mongodb:
  enabled: false
  auth:
    # Secret with the keys `username` and `password` of the app user
    existingSecret: cloud-registry-mongodb-credentials
config:
  db:
    host: mongodb.example.org
    port: 27017
```

For an installed release, setting `mongodb.enabled: false` deletes the
bundled MongoDB with its PersistentVolumeClaim (`<app>-mongodb`), and usually
its data with it: dump the data first and restore it into the external
MongoDB (see the steps of [From the legacy charts](#from-the-legacy-charts),
with the release's MongoDB Deployment, `<app>-mongodb`), or set the reclaim
policy of the volume to `Retain` before upgrading.

The app authenticates as the app user against its database (`serviceStore`),
so create that user there, with the `readWrite` role on `serviceStore` and on
the database of access control, if the config sets another one
(`config.security.access_control.db_name`; see
[Authentication](#authentication)); the Secret needs no root credentials. With
`mongodb.auth.enabled: false`, the app connects without credentials. Allow
the app's pods to connect to the database, e.g., in the database's firewall
(the chart's NetworkPolicies don't restrict connections from the app).
