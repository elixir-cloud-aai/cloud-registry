{{/*
Name of the chart, or `nameOverride`; fails if the removed
`cloud_registry.appName` is set.
*/}}
{{- define "cloud-registry.name" -}}
{{- if .Values.cloud_registry.appName -}}
{{- fail "cloud_registry.appName was removed in chart version 4: resource names are derived from the release name; set fullnameOverride to choose them (see deployment/README.md, Upgrades)" -}}
{{- end -}}
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
name is truncated first, so that the names never collide.
*/}}
{{- define "cloud-registry.mongodbFullname" -}}
{{- printf "%s-mongodb" (include "cloud-registry.fullname" . | trunc 55 | trimSuffix "-") -}}
{{- end -}}

{{/*
Labels that select the pods of a component; call with a dict of the root
context (`root`) and the component (`component`: `app` or `mongodb`).
*/}}
{{- define "cloud-registry.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cloud-registry.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
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
      name: {{ $secret }}
      key: {{ $key }}
{{- end }}
{{- end -}}
