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
