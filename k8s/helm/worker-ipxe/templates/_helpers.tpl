{{/*
Expand the name of the chart.
*/}}
{{- define "worker-ipxe.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "worker-ipxe.fullname" -}}
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
{{/*
Image ref. global.imageRegistry (umbrella) replaces the repository's registry+org, keeping the
service name; global.imageTag fills the tag when image.tag is empty.
*/}}
{{- define "worker-ipxe.image" -}}
{{- $repo := .Values.image.repository -}}
{{- $reg := dig "global" "imageRegistry" "" .Values.AsMap -}}
{{- if $reg -}}{{- $repo = printf "%s/%s" $reg (last (splitList "/" .Values.image.repository)) -}}{{- end -}}
{{- $tag := .Values.image.tag | default (dig "global" "imageTag" "" .Values.AsMap) | default .Chart.AppVersion -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{- define "worker-ipxe.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "worker-ipxe.labels" -}}
helm.sh/chart: {{ include "worker-ipxe.chart" . }}
{{ include "worker-ipxe.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "worker-ipxe.selectorLabels" -}}
app.kubernetes.io/name: {{ include "worker-ipxe.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "worker-ipxe.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "worker-ipxe.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
