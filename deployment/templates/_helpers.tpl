{{/*
Name of the existing Secret with the MongoDB credentials.
*/}}
{{- define "cloud-registry.mongodbSecret" -}}
{{- required "mongodb.auth.existingSecret must be set if mongodb.auth.enabled" .Values.mongodb.auth.existingSecret -}}
{{- end -}}
