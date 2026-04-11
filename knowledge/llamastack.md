# LlamaStack on RHOAI — Deployment Knowledge Base

## Overview

LlamaStack is the AI orchestration layer in the MaaS platform. It provides a unified
API for inference routing, RAG (vector stores, file ingestion, agents), and tool calling.
On RHOAI 3.4, LlamaStack version is **0.5.0.1+rhai0**, shipped via the Red Hat container
image `registry.redhat.io/rhoai/odh-llama-stack-core-rhel9`.

---

## Critical Discovery: Built-in Parameterized Config

The RHOAI LlamaStack image ships with a **built-in config** at `/opt/app-root/config.yaml`
that is fully parameterized via environment variables. You do NOT need a custom ConfigMap
for most deployments. The built-in `rh` distribution config reads all settings from env vars.

**This was discovered after multiple failed attempts** using custom ConfigMap-based configs.
The image merges custom configs with the built-in defaults, causing unexpected behavior
where the built-in values (e.g., `host: localhost` for Postgres) override custom values.

### Key Environment Variables

| Env Var | Purpose | Default |
|---|---|---|
| `POSTGRES_HOST` | PostgreSQL host for SQL/KV storage | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_DB` | Database name | `llamastack` |
| `POSTGRES_USER` | Database user | `llamastack` |
| `POSTGRES_PASSWORD` | Database password | `llamastack` |
| `VLLM_URL` | Single inference model URL (enables `vllm-inference` provider) | empty (disabled) |
| `VLLM_API_TOKEN` | Token for MaaS gateway auth | `fake` |
| `VLLM_TLS_VERIFY` | TLS verification for vLLM | `true` |
| `VLLM_MAX_TOKENS` | Max tokens per request | `4096` |
| `VLLM_EMBEDDING_URL` | Embedding model URL (enables `vllm-embedding` provider) | empty (disabled) |
| `VLLM_EMBEDDING_API_TOKEN` | Token for embedding endpoint | `fake` |
| `MILVUS_ENDPOINT` | Remote Milvus URI (enables `milvus-remote` provider) | empty (disabled) |
| `MILVUS_TOKEN` | Milvus auth token | empty |
| `MILVUS_CONSISTENCY_LEVEL` | Milvus consistency level (**must be set if MILVUS_ENDPOINT is set**) | empty |
| `ENABLE_S3` | Enable S3 files provider | empty (disabled) |
| `S3_BUCKET_NAME` | S3 bucket for file storage | empty |
| `AWS_ACCESS_KEY_ID` | AWS access key | empty |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key | empty |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |
| `LLAMA_STACK_CONFIG_DIR` | Writable directory for runtime state | `/opt/app-root/src/.llama/distributions/rh/` |

### Conditional Provider Activation

The built-in config uses `${env.VAR:+provider_name}` syntax for provider IDs. When the
env var is **set** (non-empty), the provider is activated with that name. When **unset**,
the provider ID becomes empty string and is effectively disabled.

Examples:
- `provider_id: ${env.VLLM_URL:+vllm-inference}` — only active when `VLLM_URL` is set
- `provider_id: ${env.MILVUS_ENDPOINT:+milvus-remote}` — only active when `MILVUS_ENDPOINT` is set
- `provider_id: milvus` — **always active** (inline Milvus Lite, unconditional)

---

## Pitfalls and Gotchas

### 1. Custom ConfigMap Gets Merged, Not Replaced

**Problem:** When you mount a custom config at `/etc/llama-stack/config.yaml` and set
`LLAMA_STACK_CONFIG=/etc/llama-stack/config.yaml`, the RHOAI image still loads its
built-in config at `/opt/app-root/config.yaml` and **merges** the two. The built-in
defaults (e.g., `host: localhost` for Postgres) can override your custom values.

**Solution:** Don't use a custom ConfigMap. Use the built-in config and set env vars.
If you must customize (e.g., for multi-model inference), overlay only the sections
that differ and ensure env var names match the built-in `${env.VAR}` references.

### 2. MILVUS_CONSISTENCY_LEVEL Must Be Set Explicitly

**Problem:** Setting `MILVUS_ENDPOINT` activates the remote Milvus provider, but
`MILVUS_CONSISTENCY_LEVEL` defaults to empty. Pydantic validation fails with:
```
ValidationError: 1 validation error for MilvusVectorIOConfig
consistency_level: Input should be a valid string, input_value=None
```

**Solution:** Always set `MILVUS_CONSISTENCY_LEVEL=Strong` when using remote Milvus.

### 3. Inline Milvus Lite Runs Alongside Remote Milvus

**Problem:** The built-in config has `provider_id: milvus` (inline Milvus Lite) with
no env var guard — it's **always enabled**. When you also enable remote Milvus via
`MILVUS_ENDPOINT`, both providers run simultaneously. The inline Milvus Lite uses a
local socket file and can produce gRPC `too_many_pings` warnings.

**Impact:** Transient gRPC warnings in logs, occasional startup delays. Not a crash —
the pod recovers after a few restart attempts.

**Mitigation:** Mount an `emptyDir` at `/opt/app-root/src/.llama` so the inline Milvus
Lite has a writable path. Both providers coexist without issues.

### 4. Writable Filesystem Required for Runtime State

**Problem:** The `inline::localfs` files provider and inline Milvus Lite both try to
write to `/opt/app-root/src/.llama/distributions/rh/`. The container filesystem is
read-only in OpenShift, causing `PermissionError`.

**Solution:** Mount two `emptyDir` volumes:
- `/opt/app-root/src/.llama` — for inline Milvus Lite and local files provider
- `/tmp/llama-runtime` — set `LLAMA_STACK_CONFIG_DIR` to this path for runtime state

### 5. Model IDs Use Provider-Prefixed Names

**Problem:** The built-in config registers models with provider-prefixed IDs like
`vllm-inference/llama-3-1-8b-instruct-fp8` instead of bare names like
`llama-3-1-8b-instruct-fp8`. The notebook-api code may reference bare names.

**Impact:** Model lookups fail if the notebook-api uses bare model names.

**Solution:** Check `/v1/models` endpoint to verify actual model IDs and update
the notebook-api's model references accordingly.

### 6. VLLM_URL Supports Only One Inference Model

**Problem:** The built-in config has a single `VLLM_URL` for one inference provider.
If you need multiple LLM models (e.g., Qwen, Llama, Mistral), you need a custom
ConfigMap with multiple `remote::vllm` provider entries — but then you hit the
merge problem described in Pitfall #1.

**Workaround for multi-model:** Use a custom ConfigMap mounted at
`/opt/app-root/config.yaml` via `subPath` to fully replace the built-in config.
This requires replicating all the built-in providers you want to keep.

---

## Storage Backend Support (Validated on v0.5.0)

### PostgreSQL — Native Support

LlamaStack 0.5.0 natively supports Postgres for both SQL and KV storage:

| Config Type | Type Value | Fields |
|---|---|---|
| `PostgresSqlStoreConfig` | `sql_postgres` | `host`, `port`, `db`, `user`, `password` |
| `PostgresKVStoreConfig` | `kv_postgres` | `host`, `port`, `db`, `user`, `password`, `ssl_mode`, `table_name` |

Both replace the default SQLite backends. A single Postgres instance handles all three
storage roles: `metadata_store`, `kv_default`, and `sql_default`.

### Remote Milvus — Native Support

| Config Type | Type Value | Fields |
|---|---|---|
| `MilvusVectorIOConfig` | `remote::milvus` | `uri`, `token`, `consistency_level`, `persistence` |

The `persistence` field references a KV backend (e.g., `kv_default`) for vector store
registry metadata.

### S3 Files — Native Support

| Config Type | Type Value | Fields |
|---|---|---|
| `S3FilesImplConfig` | `remote::s3` | `bucket_name`, `region`, `aws_access_key_id`, `aws_secret_access_key`, `endpoint_url`, `auto_create_bucket`, `metadata_store` |

The `metadata_store` field references a SQL backend for file metadata tracking.

### Available but Not Used

- `remote::pgvector` — Postgres-based vector store (alternative to Milvus)
- `remote::chroma`, `remote::qdrant`, `remote::weaviate` — other vector stores
- `kv_redis`, `kv_mongodb` — alternative KV backends

---

## Architecture: Centralized LlamaStack (rag-central)

### Design

A single stateless LlamaStack pod backed by shared external services:

| Component | Backend | Purpose |
|---|---|---|
| SQL storage | PostgreSQL (dedicated, in-namespace) | Responses, file metadata, conversations, inference log |
| KV storage | PostgreSQL (same instance) | Agent state, vector store registry, dataset metadata |
| Vector IO | Milvus standalone (dedicated, in-namespace) | Document embeddings for RAG |
| Files | AWS S3 | Raw uploaded documents (PDF, TXT, DOCX) |
| Inference | MaaS gateway (external) | LLM chat/completions via Kuadrant-protected gateway |
| Embedding | Snowflake embed (external, in llm namespace) | Vector generation for RAG ingestion |

### Why Stateless Matters

With all state in Postgres + Milvus + S3, the LlamaStack pod itself holds no data.
This enables:
- **HPA scaling** (future work) — multiple replicas serve requests concurrently
- **Zero-downtime restarts** — no data loss on pod recreation
- **GitOps-friendly** — no PVCs attached to the LlamaStack deployment

### Secrets Not in Git

Two secrets must be created by `configure.sh` or manually:

1. **`llamastack-vllm-token`** — enterprise-tier SA token for MaaS gateway access
   ```bash
   oc create token llamastack-internal \
     -n maas-default-gateway-tier-enterprise \
     --audience=maas-default-gateway-sa \
     --duration=8760h
   ```

2. **`llamastack-s3`** — AWS credentials and bucket name for S3 file storage
   ```bash
   oc create secret generic llamastack-s3 -n rag-central \
     --from-literal=bucket-name=<bucket> \
     --from-literal=region=us-east-2 \
     --from-literal=aws-access-key-id=<key> \
     --from-literal=aws-secret-access-key=<secret>
   ```

---

## Operator-Managed vs Custom Deployment

The RHOAI 3.4 LlamaStack operator creates per-user instances via
`LlamaStackDistribution` CRs in Data Science Project namespaces (GenAI Playground).
These use the same image but with operator-managed configs.

**Operator-managed pattern:**
- Config mounted at `/etc/llama-stack/config.yaml` (user config) merged with
  built-in `/opt/app-root/config.yaml`
- PVC at `/opt/app-root/src/.llama/distributions/rh/` for local state
- Token from `llamastack-vllm-token` secret in the workspace namespace
- One instance per user/project

**Our custom rag-central pattern:**
- No custom ConfigMap — relies entirely on env vars and built-in config
- No PVC — fully stateless with external backends
- Shared across all users
- Deployed via GitOps Helm chart

---

## Token Flow for LlamaStack → MaaS Gateway

LlamaStack reaches the MaaS gateway for inference using an enterprise-tier SA token.
The token is created from `llamastack-internal` SA in
`maas-default-gateway-tier-enterprise` namespace with audience `maas-default-gateway-sa`.

This bypasses all Kuadrant rate limits (enterprise tier has the highest limits).
Per-user rate limiting is handled at the notebook-api application level, not at the
LlamaStack level.

The token has a max duration of 8760h (1 year) and must be rotated before expiry.
