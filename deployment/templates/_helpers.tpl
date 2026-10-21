{{/*
Name of the chart, or `nameOverride`.
*/}}
{{- define "cloud-registry.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Name of the app's resources: the release name, followed by the chart name
unless the release name contains it, or `fullnameOverride`; truncated to 63
characters, as some names must be DNS labels.
*/}}
{{- define "cloud-registry.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "cloud-registry.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Name of the MongoDB resources and the host name of its Service; the app's
name is truncated first, so that the suffix is kept (`cloud-registry.validate`
rejects the one name that still collides).
*/}}
{{- define "cloud-registry.mongodbFullname" -}}
{{- printf "%s-mongodb" (include "cloud-registry.fullname" . | trunc 55 | trimSuffix "-") -}}
{{- end -}}

{{/*
Whether an OpenShift Route is created ("true") or not (empty): without an
Ingress, if the cluster serves Routes.
*/}}
{{- define "cloud-registry.routeEnabled" -}}
{{- if and (not .Values.cloud_registry.ingress.enabled) (.Capabilities.APIVersions.Has "route.openshift.io/v1") -}}
true
{{- end -}}
{{- end -}}

{{/*
Checks of the values and the release; renders nothing. Included once (in the
app's ConfigMap template).
*/}}
{{- define "cloud-registry.validate" -}}
{{- if .Values.cloud_registry.appName -}}
{{- fail "cloud_registry.appName was removed after the legacy charts (versions 2 and 3): resource names are derived from the release name; set fullnameOverride to choose them at installation (see deployment/README.md, Names and labels)" -}}
{{- end -}}
{{- $fullname := include "cloud-registry.fullname" . -}}
{{- if eq $fullname (include "cloud-registry.mongodbFullname" .) -}}
{{- fail (printf "the app and MongoDB resources would both be named %s: choose a fullnameOverride that does not end in -mongodb" $fullname) -}}
{{- end -}}
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
resources, of any chart version); call with a dict of the root context (`root`), the
resource's kind (`kind`) and the resource (`resource`, empty if none).
*/}}
{{- define "cloud-registry.failIfLegacy" -}}
{{- $owner := dig "metadata" "annotations" "meta.helm.sh/release-name" "" .resource -}}
{{- $labels := dig "metadata" "labels" dict .resource -}}
{{- if and (eq $owner .root.Release.Name) (not (hasKey $labels "app.kubernetes.io/instance")) -}}
{{- fail (printf "release %s was installed with a legacy chart (version 2 or 3), and upgrading it would delete its MongoDB data (%s mongodb): uninstall it and install the current chart instead, keeping the data as described in deployment/README.md (Upgrades, From the legacy charts)" .root.Release.Name .kind) -}}
{{- end -}}
{{- end -}}

{{/*
Labels that select the pods of a component; call with a dict of the root
context (`root`) and the component (`component`: `app` or `mongodb`).
*/}}
{{- define "cloud-registry.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloud-registry.name" .root | quote }}
app.kubernetes.io/instance: {{ .root.Release.Name | quote }}
app.kubernetes.io/component: {{ .component | quote }}
{{- end -}}

{{/*
Labels of all resources of a component; call like
`cloud-registry.selectorLabels`.
*/}}
{{- define "cloud-registry.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "cloud-registry.selectorLabels" . }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{/*
Security context from the values; call with a dict of the root context
(`root`) and the context (`context`). On OpenShift, which assigns user and
group IDs from the namespace's range (and rejects others under the default
security context constraints), these IDs are omitted.
*/}}
{{- define "cloud-registry.securityContext" -}}
{{- $context := .context | default dict -}}
{{- if .root.Capabilities.APIVersions.Has "security.openshift.io/v1" -}}
{{- $context = omit $context "runAsUser" "runAsGroup" "fsGroup" -}}
{{- end -}}
{{- toYaml $context -}}
{{- end -}}

{{/*
Name of the existing Secret with the MongoDB credentials.
*/}}
{{- define "cloud-registry.mongodbSecret" -}}
{{- required "mongodb.auth.existingSecret must be set if mongodb.auth.enabled; for existing databases without authentication, create the users first (see deployment/README.md, Authentication)" .Values.mongodb.auth.existingSecret -}}
{{- end -}}

{{/*
Environment variables taken from keys of the Secret with the MongoDB
credentials; call with a dict of the root context (`root`) and a dict mapping
variable names to Secret keys (`vars`); variables are rendered in
alphabetical order.
*/}}
{{- define "cloud-registry.mongodbSecretEnv" -}}
{{- $secret := include "cloud-registry.mongodbSecret" .root -}}
{{- range $name, $key := .vars }}
- name: {{ $name }}
  valueFrom:
    secretKeyRef:
      name: {{ $secret | quote }}
      key: {{ $key }}
{{- end }}
{{- end -}}

{{/*
App image: `cloud_registry.image` with `cloud_registry.tag`, by default the
chart's `appVersion`; an image with a tag or digest is used as it is.
*/}}
{{- define "cloud-registry.image" -}}
{{- $image := .Values.cloud_registry.image -}}
{{- $tag := .Values.cloud_registry.tag -}}
{{- if or (contains "@" $image) (regexMatch ":[^/]+$" $image) -}}
{{- if $tag -}}
{{- fail "cloud_registry.tag must not be set if cloud_registry.image includes a tag or digest" -}}
{{- end -}}
{{- $image -}}
{{- else -}}
{{- printf "%s:%s" $image ($tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{/*
Size limit of request bodies of the app, in bytes: `custom.max_content_length`
of the config, by default that of the app (1 MiB; cf.
`cloud_registry/config.yaml`). Fails unless it is a positive integer (numbers
from values files are floats), as 0 means no limit for ingress-nginx.
*/}}
{{- define "cloud-registry.maxContentLength" -}}
{{- $custom := dig "custom" dict (.Values.config | default dict) -}}
{{- if not (kindIs "map" $custom) -}}
{{- $custom = dict -}}
{{- end -}}
{{- $limit := 1048576 -}}
{{- if hasKey $custom "max_content_length" -}}
{{- $limit = index $custom "max_content_length" -}}
{{- end -}}
{{- $integer := or (kindIs "int" $limit) (kindIs "int64" $limit) (and (kindIs "float64" $limit) (eq $limit (floor $limit))) -}}
{{- if not (and $integer (ge (int64 $limit) 1)) -}}
{{- fail (printf "config.custom.max_content_length must be a positive integer (bytes), not %v" $limit) -}}
{{- end -}}
{{- int64 $limit -}}
{{- end -}}

{{/*
`emptyDir` volume source with an optional size limit (a quantity string or a
number of bytes; numbers from values files are floats, so they are rendered
as integers); none if `null`.
*/}}
{{- define "cloud-registry.emptyDir" -}}
{{- if kindIs "invalid" . -}}
emptyDir: {}
{{- else -}}
emptyDir:
  sizeLimit: {{ ternary . (int64 . | toString) (kindIs "string" .) | quote }}
{{- end -}}
{{- end -}}

{{/*
Gunicorn options (`GUNICORN_CMD_ARGS`) from `cloud_registry.gunicorn`: the
numbers (floats from values files) as integers, followed by `extraArgs`, each
in single quotes, as Gunicorn splits the options like shell words (the schema
rejects single quotes in them).
*/}}
{{- define "cloud-registry.gunicornArgs" -}}
{{- $g := .Values.cloud_registry.gunicorn -}}
{{- $args := list
  "--workers" (int $g.workers)
  "--threads" (int $g.threads)
  "--timeout" (int $g.timeout)
  "--graceful-timeout" (int $g.gracefulTimeout)
  "--keep-alive" (int $g.keepalive)
-}}
{{- range $g.extraArgs -}}
{{- include "cloud-registry.checkGunicornArg" . -}}
{{- $args = append $args (squote .) -}}
{{- end -}}
{{- join " " $args -}}
{{- end -}}

{{/*
Fail if an item of `cloud_registry.gunicorn.extraArgs` sets an option that
the chart or the app relies on: `--bind`/`-b` (the chart's ports),
`--preload` (MongoDB clients must be created after forking) or
`--graceful-timeout` (the termination grace period is derived from
`gracefulTimeout`). As Gunicorn parses them: long options also abbreviated
and with `=value`; `-b` also with an attached value and after the short
flags `-D` and `-R`. Items like `--bind x` (a space before any `=`) are
values for Gunicorn, not options, so they pass.
*/}}
{{- define "cloud-registry.checkGunicornArg" -}}
{{- $name := lower (regexReplaceAll "=.*$" . "") -}}
{{- $reserved := false -}}
{{- if hasPrefix "--" $name -}}
{{- range list "--bind" "--preload" "--graceful-timeout" -}}
{{- if and (ge (len $name) 3) (hasPrefix $name .) -}}
{{- $reserved = true -}}
{{- end -}}
{{- end -}}
{{- else if regexMatch "^-[DR]*b" . -}}
{{- $reserved = true -}}
{{- end -}}
{{- if $reserved -}}
{{- fail (printf "cloud_registry.gunicorn.extraArgs must not set --bind/-b (the chart's ports are 8080), --preload (MongoDB clients must be created after forking) or --graceful-timeout (set gracefulTimeout instead): %s" .) -}}
{{- end -}}
{{- end -}}

{{/*
Termination grace period of the app's pod, in seconds: the `preStop` delay,
then Gunicorn's graceful timeout for finishing requests, plus a margin for
Gunicorn to stop its workers and exit before it is killed.
*/}}
{{- define "cloud-registry.terminationGracePeriod" -}}
{{- $margin := 10 -}}
{{- add (int .Values.cloud_registry.preStopSleepSeconds) (int .Values.cloud_registry.gunicorn.gracefulTimeout) $margin -}}
{{- end -}}
