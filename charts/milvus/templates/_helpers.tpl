{{/* Milvus component name */}}
{{- define "milvus.fullname" -}}
milvus
{{- end }}

{{/* etcd service name */}}
{{- define "milvus.etcdServiceName" -}}
milvus-etcd
{{- end }}

{{/* MinIO service name */}}
{{- define "milvus.minioServiceName" -}}
milvus-minio
{{- end }}

{{/* Common labels */}}
{{- define "milvus.labels" -}}
app.kubernetes.io/part-of: milvus
app.kubernetes.io/managed-by: Helm
{{- end }}
