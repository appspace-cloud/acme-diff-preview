{{/*
Expand the name of the chart.
*/}}
{{- define "acme-diff-preview.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Uses fullnameOverride to keep resource names stable (e.g. the ArgoCD Helm chart
extraPaths configuration references "acme-diff-preview" as the Service backend name).
*/}}
{{- define "acme-diff-preview.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "acme-diff-preview.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "acme-diff-preview.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "acme-diff-preview.selectorLabels" -}}
app.kubernetes.io/name: {{ include "acme-diff-preview.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
component: {{ include "acme-diff-preview.name" . }}
{{- end }}
