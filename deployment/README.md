# Helm chart

## Quick start

1. Edit `values.yaml`. It is important to change `host_name` to the URL you want the application to respond to.

2. Install the Helm chart. Run this from the `deployment` folder:
```
helm install cloud-registry . -n namespace
```
`namespace` is the name of the namespace where the cluster should be installed.
`cloud-registry` could be any name, it will be referenced later for upgrading or uninstalling.

## Upgrades

If you need to change any parameter of the deployment, the procedure is similar to installing:

1. Edit `values.yaml` with the new configuration you want to apply. For example, if you want to
   change the image of cloud-registry, change `cloud_registry.image`.

2. Upgrade the Helm chart. Run this from the `deployment` folder:
```
helm upgrade cloud-registry . -n namespace
```
`namespace` is the name of the namespace where the cluster was installed.
`cloud-registry` is the name given when installing. In order to see the correct name, do a `helm ls
-n namespace`.

### Upgrading to a new app config

The app config is generated once, at installation, by the `configmap-job-*`
Job from the config in the image, and `helm upgrade` does not run the Job
again. When a new image changes the config format, such as the upgrade to
FOCA 0.14 and the removal of `meta_version`, an old config makes the app fail
at startup. In that case, delete the Job before upgrading, so that it
generates the config from the new image:

```
kubectl delete job configmap-job-<appName> -n namespace
helm upgrade cloud-registry . -n namespace
kubectl wait --for=condition=complete job/configmap-job-<appName> -n namespace
kubectl rollout restart deployment/<appName> -n namespace
```

`<appName>` is `cloud_registry.appName` in `values.yaml`. The restart is
needed because the app reads the config only at startup.
