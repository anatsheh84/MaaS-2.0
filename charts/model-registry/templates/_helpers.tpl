{{/* Postgres service name for a registry instance */}}
{{- define "model-registry.postgresServiceName" -}}
{{- printf "%s-postgres" . -}}
{{- end }}

{{/* Postgres secret name for a registry instance */}}
{{- define "model-registry.postgresSecretName" -}}
{{- printf "%s-postgres" . -}}
{{- end }}
