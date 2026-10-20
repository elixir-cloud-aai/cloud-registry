{{/*
Name of the existing Secret with the MongoDB credentials.
*/}}
{{- define "cloud-registry.mongodbSecret" -}}
{{- required "mongodb.auth.existingSecret must be set if mongodb.auth.enabled" .Values.mongodb.auth.existingSecret -}}
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
