{{/*
Settings of the app for the templates of the library chart foca-common (see
`foca-common.checkSettings` and "Helm library chart" in FOCA's docs), as
YAML; the templates are called with a dict of the root context (`root`) and
these settings (`settings`). They follow the base config in the image
(`cloud_registry/config.yaml`): its size limit of request bodies
(`server.max_content_length`), its token issuers (so an audience is
required), its database (`db.dbs`) and that of access control
(`security.access_control.db_name`). The generated config sets the public
URL of the app from `hostName`, with HTTPS on port 443, as TLS is usually
terminated by the ingress, the route or a proxy in front of the cluster.
Every template includes the settings, so the app's checks
(`cloud-registry.validate`) run first, before those of the library and
whichever template Helm renders first.
*/}}
{{- define "cloud-registry.settings" -}}
{{- include "cloud-registry.validate" . -}}
valuesKey: cloudRegistry
maxContentLength: 1048576
requireAudience: true
mongodb:
  databases: [serviceStore]
  accessControlDatabase: serviceStore
config:
  custom:
    endpoints:
      service:
        url_prefix: https
        external_host: {{ .Values.hostName | toJson }}
        external_port: 443
{{- end -}}

{{/*
Checks of the values and the release specific to this app; renders nothing.
Included by the settings (`cloud-registry.settings`), so in every template,
before the library's checks (`foca-common.validate`); the checks of renamed
and removed values come first, so that they are reported instead of their
consequences (e.g., a missing `hostName` for `host_name`).
*/}}
{{- define "cloud-registry.validate" -}}
{{- range $key := list "cloud_registry" "cloudRegistry" -}}
{{- $values := index $.Values $key -}}
{{- if and (kindIs "map" $values) (index $values "appName") -}}
{{- fail (printf "%s.appName was removed after the legacy charts (versions 2 and 3): resource names are derived from the release name; set fullnameOverride to choose them at installation (see deployment/README.md, Names and labels)" $key) -}}
{{- end -}}
{{- end -}}
{{- /*
Keys renamed to camelCase, following Helm's conventions; the old keys would
otherwise be ignored
*/ -}}
{{- range $old, $new := dict "host_name" "hostName" "cloud_registry" "cloudRegistry" -}}
{{- if hasKey $.Values $old -}}
{{- fail (printf "%s was renamed to %s (camelCase, following Helm conventions): rename it in the values file, and upgrade without --reuse-values (see deployment/README.md, To app version 0.3.0)" $old $new) -}}
{{- end -}}
{{- end -}}
{{- $config := .Values.config | default dict -}}
{{- /*
Settings of the custom config replaced by FOCA's (with the FOCA 0.15
upgrade), which the app rejects
*/ -}}
{{- $custom := dig "custom" dict $config -}}
{{- if kindIs "map" $custom -}}
{{- if hasKey $custom "max_content_length" -}}
{{- fail "config.custom.max_content_length was removed with the FOCA 0.15 upgrade: set config.server.max_content_length instead (see deployment/README.md, Ingress)" -}}
{{- end -}}
{{- $endpoints := index $custom "endpoints" -}}
{{- if and (kindIs "map" $endpoints) (hasKey $endpoints "service_info") -}}
{{- fail "config.custom.endpoints.service_info was removed with the FOCA 0.15 upgrade: set config.service_info.service instead (see deployment/README.md, Configuration)" -}}
{{- end -}}
{{- end -}}
{{- /*
As the app on startup, refuse to render without a group mapped to the role
`admin`, unless admins are explicitly not required (`custom: null` is like
no such section, see `foca-common.configOverlay`)
*/ -}}
{{- $mapping := dig "security" "access_control" "group_mapping" dict $config -}}
{{- $requireAdmin := true -}}
{{- if kindIs "map" $custom -}}
{{- $requireAdmin = dig "access_control" "require_admin" true $custom -}}
{{- end -}}
{{- if and $requireAdmin (not (has "admin" (values $mapping))) -}}
{{- fail "config.security.access_control.group_mapping must map a group to the role admin (e.g., the admin group of the identity provider), or set config.custom.access_control.require_admin to false" -}}
{{- end -}}
{{- $_ := required "hostName must be set" .Values.hostName -}}
{{- /*
Upgrades of releases of the legacy charts (versions 2 and 3, installed from
the repository before charts were released from Git tags) would delete their
MongoDB volume (`mongodb`), as its name changed. `lookup` finds nothing in
`helm template` and client-side dry runs.
*/ -}}
{{- if .Release.IsUpgrade -}}
{{- range $old := list (list "v1" "PersistentVolumeClaim") (list "apps/v1" "Deployment") -}}
{{- include "cloud-registry.failIfLegacy" (dict
  "root" $
  "kind" (index $old 1)
  "resource" (lookup (index $old 0) (index $old 1) $.Release.Namespace "mongodb")
) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Fails if a resource named `mongodb` (looked up in the cluster) belongs to
this release, but was created by a legacy chart (version 2 or 3): without the
label `app.kubernetes.io/instance` (Helm itself adds `managed-by` to all
resources, of any chart version); call with a dict of the root context
(`root`), the resource's kind (`kind`) and the resource (`resource`, empty if
none).
*/}}
{{- define "cloud-registry.failIfLegacy" -}}
{{- $owner := dig "metadata" "annotations" "meta.helm.sh/release-name" "" .resource -}}
{{- $labels := dig "metadata" "labels" dict .resource -}}
{{- if and (eq $owner .root.Release.Name) (not (hasKey $labels "app.kubernetes.io/instance")) -}}
{{- fail (printf "release %s was installed with a legacy chart (version 2 or 3), and upgrading it would delete its MongoDB data (%s mongodb): uninstall it and install the current chart instead, keeping the data as described in deployment/README.md (Upgrades, From the legacy charts)" .root.Release.Name .kind) -}}
{{- end -}}
{{- end -}}
