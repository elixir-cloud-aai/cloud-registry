{{/*
Settings of the app for the templates of the library chart foca-common (see
`foca-common.checkSettings` and "Helm library chart" in FOCA's docs), as
YAML; the templates are called with a dict of the root context (`root`) and
these settings (`settings`). They follow the base config in the image
(`cloud_registry/config.yaml`): its size limit of request bodies
(`server.max_content_length`), its token issuers (so an audience is
required, unless each issuer has one, cf. `cloud-registry.requireAudience`),
its database (`db.dbs`) and that of access control
(`security.access_control.db_name`). The generated config sets the public
URL of the app (`server.public_url`) to `publicUrl`, or else from
`hostName`, with HTTPS, as TLS is usually terminated by the ingress, the
route or a proxy in front of the cluster.
Every template includes the settings, so the app's checks
(`cloud-registry.validate`) run first, before those of the library and
whichever template Helm renders first.
*/}}
{{- define "cloud-registry.settings" -}}
{{- include "cloud-registry.validate" . -}}
valuesKey: cloudRegistry
maxContentLength: 1048576
requireAudience: {{ include "cloud-registry.requireAudience" . }}
mongodb:
  databases: [serviceStore]
  accessControlDatabase: serviceStore
config:
  server:
    public_url: {{ .Values.publicUrl | default (printf "https://%s/" .Values.hostName) | toJson }}
  # Empty, so that `config.custom: null` is like no custom config (merged
  # into this), not a null section, which the app would refuse
  custom: {}
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
{{- fail (printf "%s was renamed to %s (camelCase, following Helm conventions): rename it in the values file, and upgrade without --reuse-values (see deployment/README.md#to-app-version-040)" $old $new) -}}
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
{{- /* Replaced by FOCA's (with the FOCA 0.18 upgrade) */ -}}
{{- if and (kindIs "map" $endpoints) (hasKey $endpoints "service") -}}
{{- fail "config.custom.endpoints.service was removed with the FOCA 0.18 upgrade: set publicUrl instead, to the public URL of the app if not https://<hostName>/, e.g., with the path prefix of a proxy (see deployment/README.md#to-app-version-060)" -}}
{{- end -}}
{{- $customAccessControl := index $custom "access_control" -}}
{{- if and (kindIs "map" $customAccessControl) (hasKey $customAccessControl "require_admin") -}}
{{- fail "config.custom.access_control.require_admin was removed with the FOCA 0.18 upgrade: set config.security.access_control.require_admin instead (see deployment/README.md#to-app-version-060)" -}}
{{- end -}}
{{- end -}}
{{- /*
As the app on startup, refuse to render without a group mapped to the role
`admin`, unless admins are explicitly not required
(`security.access_control.require_admin`, FOCA's; `null` sections are like
no such section, see `foca-common.configOverlay`)
*/ -}}
{{- $mapping := dict -}}
{{- $requireAdmin := "true" -}}
{{- $claimGroups := "" -}}
{{- $issuers := list -}}
{{- $security := get $config "security" -}}
{{- if kindIs "map" $security -}}
{{- $securityAccessControl := get $security "access_control" -}}
{{- if kindIs "map" $securityAccessControl -}}
{{- /* Only if set, as `get` returns an empty string for missing keys */ -}}
{{- if hasKey $securityAccessControl "group_mapping" -}}
{{- $mapping = get $securityAccessControl "group_mapping" -}}
{{- end -}}
{{- if hasKey $securityAccessControl "require_admin" -}}
{{- $requireAdmin = include "cloud-registry.parseBool" (dict
  "value" (get $securityAccessControl "require_admin")
  "key" "config.security.access_control.require_admin"
) -}}
{{- end -}}
{{- if hasKey $securityAccessControl "claim_groups" -}}
{{- $claimGroups = get $securityAccessControl "claim_groups" -}}
{{- if kindIs "invalid" $claimGroups -}}
{{- $claimGroups = "" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $auth := get $security "auth" -}}
{{- if kindIs "map" $auth -}}
{{- $issuers = get $auth "issuers" | default list -}}
{{- end -}}
{{- end -}}
{{- /*
Checked in the order of the app's checks: the issuers, the group claim and
the groups mapped to roles. Mirrors FOCA's check on startup (access control
needs trusted token issuers): refuse to render without a list of non-blank
issuers, as the base config trusts none; issuers are compared with the `iss`
claim as written, so surrounding whitespace is refused, too. Issuers are
URLs, or maps with the URL (`issuer`) and settings of their own (FOCA's
`IssuerConfig`, which the app validates), whose group claims
(`claim_groups`) replace the global one for their tokens: as in the app,
group mappings need a group claim of at least one issuer
*/ -}}
{{- $issuersError := "config.security.auth.issuers must list the trusted token issuers (the iss claim of their tokens, e.g., https://login.example.org/oidc/, without surrounding whitespace; or maps of it, as issuer, and settings of the issuer): the app trusts none by default" -}}
{{- if not (and (kindIs "slice" $issuers) $issuers) -}}
{{- fail $issuersError -}}
{{- end -}}
{{- $anyClaim := false -}}
{{- range $issuer := $issuers -}}
{{- $claim := $claimGroups -}}
{{- if kindIs "map" $issuer -}}
{{- if hasKey $issuer "claim_groups" -}}
{{- $claim = get $issuer "claim_groups" -}}
{{- end -}}
{{- $issuer = get $issuer "issuer" -}}
{{- end -}}
{{- if and (kindIs "string" $claim) $claim (eq $claim (trim $claim)) -}}
{{- $anyClaim = true -}}
{{- end -}}
{{- if not (and (kindIs "string" $issuer) (trim $issuer) (eq $issuer (trim $issuer))) -}}
{{- fail $issuersError -}}
{{- end -}}
{{- end -}}
{{- /* The claim is looked up as written, as by the app */ -}}
{{- if not (kindIs "string" $claimGroups) -}}
{{- fail "config.security.access_control.claim_groups must be the name of a claim (a string)" -}}
{{- end -}}
{{- if ne $claimGroups (trim $claimGroups) -}}
{{- fail "config.security.access_control.claim_groups must be the name of a claim, without surrounding whitespace" -}}
{{- end -}}
{{- if kindIs "invalid" $mapping -}}
{{- $mapping = dict -}}
{{- else if not (kindIs "map" $mapping) -}}
{{- fail "config.security.access_control.group_mapping must be a map of groups to roles" -}}
{{- end -}}
{{- /*
As the app on startup, refuse to render group mappings without the claim
with the groups of users, as no mapping would apply
*/ -}}
{{- if and $mapping (not $anyClaim) -}}
{{- fail "config.security.access_control.claim_groups must name the claim with the groups of users (e.g., groups), as groups are mapped to roles (group_mapping); also add it to config.security.auth.userinfo_claims if the identity provider releases it via user info only" -}}
{{- end -}}
{{- if and (eq $requireAdmin "true") (not (has "admin" (values $mapping))) -}}
{{- fail "config.security.access_control.group_mapping must map a group to the role admin (e.g., the admin group of the identity provider), or set config.security.access_control.require_admin to false" -}}
{{- end -}}
{{- $_ := required "hostName must be set" .Values.hostName -}}
{{- end -}}

{{/*
Whether an audience is required (`requireAudience` of the settings; renders
"true" or "false"): unless each trusted issuer has an audience of its own
(a map with a list of non-blank `audience`s), which replaces the global one
(`security.auth.audience`) for its tokens, as in FOCA's `IssuerConfig`.
*/}}
{{- define "cloud-registry.requireAudience" -}}
{{- $issuers := dig "security" "auth" "issuers" list (.Values.config | default dict) -}}
{{- $own := and (kindIs "slice" $issuers) (gt (len $issuers) 0) -}}
{{- range $issuer := (ternary $issuers (list) $own) -}}
{{- $audience := and (kindIs "map" $issuer) (get $issuer "audience") -}}
{{- if not (and (kindIs "slice" $audience) (gt (len $audience) 0)) -}}
{{- $own = false -}}
{{- else -}}
{{- range $client := $audience -}}
{{- if not (and (kindIs "string" $client) (trim $client)) -}}
{{- $own = false -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- not $own -}}
{{- end -}}

{{/*
A boolean of the config, parsed as pydantic parses booleans (lax mode), as the
app does: `true` or `false`, the strings `1`, `on`, `t`, `true`, `y`, `yes`
and `0`, `off`, `f`, `false`, `n`, `no` (in any case, without whitespace),
and the numbers 1 and 0. Renders "true" or "false"; fails otherwise. Call
with a dict of the value (`value`) and its key (`key`).
*/}}
{{- define "cloud-registry.parseBool" -}}
{{- $value := .value -}}
{{- $text := lower (toString $value) -}}
{{- $isNumber := or (kindIs "float64" $value) (kindIs "int64" $value) (kindIs "int" $value) -}}
{{- if kindIs "bool" $value -}}
{{- $value -}}
{{- else if and (kindIs "string" $value) (has $text (list "1" "on" "t" "true" "y" "yes")) -}}
true
{{- else if and (kindIs "string" $value) (has $text (list "0" "off" "f" "false" "n" "no")) -}}
false
{{- else if and $isNumber (has $text (list "0" "1")) -}}
{{- eq $text "1" -}}
{{- else if kindIs "invalid" $value -}}
{{- fail (printf "%s must be true or false, not null" .key) -}}
{{- else -}}
{{- fail (printf "%s must be true or false, not %v" .key $value) -}}
{{- end -}}
{{- end -}}

{{/*
Fails on upgrades of releases of the legacy charts (versions 2 and 3,
installed from the repository before charts were released from Git tags),
which would delete their MongoDB volume (`mongodb`), as its name changed;
renders nothing. Included once (in the app's ConfigMap template), as it looks
up resources in the cluster. `lookup` finds nothing in `helm template` and
client-side dry runs.
*/}}
{{- define "cloud-registry.checkUpgrade" -}}
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
