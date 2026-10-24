{{/*
Common labels applied to umbrella-owned resources (postgres, redis, policies, secrets).
*/}}
{{- define "gough.labels" -}}
app.kubernetes.io/part-of: gough
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{/*
Returns the existing value for .key from .data (base64 from a lookup) or a fresh random one of .length chars.
Keeps credentials stable across helm upgrades.
*/}}
{{- define "gough.keepOrGenerate" -}}
{{- $old := get .data .key -}}
{{- if $old -}}
{{- b64dec $old -}}
{{- else -}}
{{- randAlphaNum (int .length) -}}
{{- end -}}
{{- end }}

{{/*
Fails the render when the image tag is not set (no mutable/implicit default for hub images).
*/}}
{{- define "gough.requireImageTag" -}}
{{- if not .Values.global.imageTag -}}
{{- fail "global.imageTag is required: set it in the env file or pass --set global.imageTag=<tag>" -}}
{{- end -}}
{{- end }}
