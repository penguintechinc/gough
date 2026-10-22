{{/*
Expand the name of the chart.
*/}}
{{- define "api-manager.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "api-manager.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "api-manager.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "api-manager.labels" -}}
helm.sh/chart: {{ include "api-manager.chart" . }}
{{ include "api-manager.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "api-manager.selectorLabels" -}}
app.kubernetes.io/name: {{ include "api-manager.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "api-manager.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "api-manager.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Extra env, volumes and volumeMounts driven by umbrella globals (global.lxd, global.bootstrapToken).
Returns JSON {env, volumes, volumeMounts}; empty lists when unset.
*/}}
{{- define "api-manager.extra" -}}
{{- $env := list -}}
{{- $vols := list -}}
{{- $mounts := list -}}
{{- $bt := dig "global" "bootstrapToken" (dict) .Values.AsMap -}}
{{- $bootSecret := $bt.existingSecret | default "gough-bootstrap-token" -}}
{{- $ttl := int ($bt.ttlHours | default 24) -}}
{{- if or (lt $ttl 1) (gt $ttl 24) -}}
{{- fail "global.bootstrapToken.ttlHours must be between 1 and 24" -}}
{{- end -}}
{{- $env = append $env (dict "name" "GOUGH_BOOTSTRAP_TOKEN" "valueFrom" (dict "secretKeyRef" (dict "name" $bootSecret "key" "token" "optional" true))) -}}
{{- $env = append $env (dict "name" "GOUGH_BOOTSTRAP_TOKEN_TTL_HOURS" "value" (toString $ttl)) -}}
{{- $lxd := dig "global" "lxd" (dict) .Values.AsMap -}}
{{- if $lxd.apiUrl -}}
  {{- if not (hasPrefix "https://" $lxd.apiUrl) -}}
    {{- fail "global.lxd.apiUrl must start with https://" -}}
  {{- end -}}
  {{- if not $lxd.clientCertSecretRef.name -}}
    {{- fail "global.lxd.clientCertSecretRef.name is required when global.lxd.apiUrl is set" -}}
  {{- end -}}
  {{- $env = append $env (dict "name" "LXD_API_URL" "value" $lxd.apiUrl) -}}
  {{- $env = append $env (dict "name" "LXD_CLIENT_CERT" "value" "/etc/gough/lxd/tls.crt") -}}
  {{- $env = append $env (dict "name" "LXD_CLIENT_KEY" "value" "/etc/gough/lxd/tls.key") -}}
  {{- $verify := "true" -}}
  {{- if hasKey $lxd "verifySsl" -}}{{- $verify = toString $lxd.verifySsl -}}{{- end -}}
  {{- $env = append $env (dict "name" "LXD_VERIFY_SSL" "value" $verify) -}}
  {{- $vols = append $vols (dict "name" "lxd-client" "secret" (dict "secretName" $lxd.clientCertSecretRef.name "defaultMode" 256)) -}}
  {{- $mounts = append $mounts (dict "name" "lxd-client" "mountPath" "/etc/gough/lxd" "readOnly" true) -}}
  {{- if $lxd.caSecretRef.name -}}
    {{- $env = append $env (dict "name" "LXD_CA_FILE" "value" "/etc/gough/lxd-ca/ca.crt") -}}
    {{- $vols = append $vols (dict "name" "lxd-ca" "secret" (dict "secretName" $lxd.caSecretRef.name "defaultMode" 256 "items" (list (dict "key" "ca.crt" "path" "ca.crt")))) -}}
    {{- $mounts = append $mounts (dict "name" "lxd-ca" "mountPath" "/etc/gough/lxd-ca" "readOnly" true) -}}
  {{- end -}}
{{- end -}}
{{- toJson (dict "env" $env "volumes" $vols "volumeMounts" $mounts) -}}
{{- end }}
