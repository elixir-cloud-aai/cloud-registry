# Helm chart

## Configuration

The image contains the app's complete base config
([`cloud_registry/config.yaml`](../cloud_registry/config.yaml)). The chart
renders a config overlay into the `<appName>-config` ConfigMap, which FOCA
merges into the base config on startup (via `FOCA_CONFIG_OVERLAY`):

- `host_name` sets the public URL of the app
  (`custom.endpoints.service`: `https://<host_name>:443`);
- `config` overrides any key of the base config; it takes precedence over the
  values generated from `host_name`. Mappings are merged recursively, all
  other values (including lists) replace those of the base config.

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
upgrade.

## Installation

Install the chart from the `deployment` folder:

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
