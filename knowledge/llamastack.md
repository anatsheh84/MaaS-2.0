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

The built-in config registers models with provider-prefixed IDs like
`vllm-inference/llama-3-1-8b-instruct-fp8` instead of bare names like
`llama-3-1-8b-instruct-fp8`. Any client calling the LlamaStack API must use
these prefixed names. Check `/v1/models` endpoint to verify actual IDs.

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

## Token Flow: How LlamaStack Reaches Inference Models

LlamaStack calls the MaaS gateway to reach inference models. The token it uses
determines the user identity and tier for Kuadrant rate limiting.

### Two Deployment Patterns — Two Token Flows

**1. Operator-managed LlamaStack (GenAI Playground — per-user instances)**

Each user gets their own LlamaStack instance in their Data Science Project namespace.
The token flow preserves per-user identity:

```
User Browser → OpenShift OAuth → data-science-gateway (EnvoyFilter)
    → gen-ai-ui (extracts user OAuth token)
    → LlamaStack (receives token via X-LlamaStack-Provider-Data header)
        → LlamaStack's remote::vllm provider uses this token as the API key
        → MaaS gateway (Kuadrant does kubernetesTokenReview on the user's token)
            → Rate limits applied based on user's tier (free/premium/enterprise)
            → vLLM model pod
```

Key mechanism: The `X-LlamaStack-Provider-Data: {"vllm_api_token": "<user-token>"}`
header **overrides** the config's `api_token` (which defaults to `fake`). This means
each user's requests are individually rate-limited by Kuadrant based on their own
OpenShift identity and group membership.

**2. Custom centralized LlamaStack (rag-central — shared instance)**

A single LlamaStack pod serves all users. It uses a **static enterprise-tier SA token**
configured via `VLLM_API_TOKEN` env var:

```
Any client → LlamaStack (rag-central namespace)
    → LlamaStack's remote::vllm provider uses the static enterprise token
    → MaaS gateway (Kuadrant sees the enterprise SA identity)
        → Enterprise tier rate limits apply (100K tokens/min)
        → vLLM model pod
```

**Important implication:** In the centralized pattern, ALL requests from ALL users
appear as the same enterprise-tier identity to Kuadrant. Per-user rate limiting
does NOT happen at the gateway level. If per-user limits are needed, they must be
implemented at the application layer (the client calling LlamaStack), or the client
must inject per-user tokens via `X-LlamaStack-Provider-Data` header.

### Token Creation

The enterprise-tier SA token is created from `llamastack-internal` ServiceAccount
in the `maas-default-gateway-tier-enterprise` namespace:

```bash
oc create token llamastack-internal \
  -n maas-default-gateway-tier-enterprise \
  --audience=maas-default-gateway-sa \
  --duration=8760h
```

The `--audience=maas-default-gateway-sa` must match the audience configured in
Kuadrant's AuthPolicy `kubernetesTokenReview`. Max duration is 8760h (1 year).

### X-LlamaStack-Provider-Data Header

This header allows **per-request token override** without changing the LlamaStack
config. The header value is a JSON object:

```json
{"vllm_api_token": "<per-user-token>"}
```

LlamaStack's `_get_api_key_from_config_or_provider_data()` checks for this header
on every inference request. If present, the per-user token is used instead of the
config's static `api_token`. This is the mechanism that enables per-user rate limiting
even with a shared LlamaStack instance.

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
| Embedding | Nomic Embed v1.5 (external, in llm namespace) | 768-dim vector generation for RAG ingestion |

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
- Per-user tokens injected via `X-LlamaStack-Provider-Data` header by gen-ai-ui
- One instance per user/project
- Per-user rate limiting works because each user's own OAuth token is used

**Our custom rag-central pattern:**
- No custom ConfigMap — relies entirely on env vars and built-in config
- No PVC — fully stateless with external backends (Postgres, Milvus, S3)
- Shared across all users
- Static enterprise-tier token — all requests appear as same identity to Kuadrant
- Per-user rate limiting requires application-layer implementation or
  `X-LlamaStack-Provider-Data` token injection by the calling client
- Deployed via GitOps Helm chart


---

## RHOAI 3.4 Session Findings (April 2026)

### Embedding Model: Nomic Embed v1.5 replaces Snowflake Arctic

**Why the switch:** Snowflake Arctic Embed L v2.0 has `matryoshka_dimensions: [256]`
in its HuggingFace config. vLLM 0.13 (RHOAI 3.3.0) validates any explicit `dimensions`
parameter against this list. LlamaStack 0.5.0 sends `dimensions=N` during embedding
ingestion (from the model's `embedding_dimension` metadata) but does NOT send it during
search queries. This creates a dimension mismatch:

- Ingestion: `dimensions=1024` → vLLM rejects (only [256] allowed for matryoshka)
- Ingestion: `dimensions=256` → vLLM accepts, stores 256-dim vectors
- Search: no `dimensions` param → vLLM returns 1024-dim → Milvus rejects (mismatch)

**This is a confirmed LlamaStack bug** — inconsistent dimension handling between
ingestion and search code paths.

**Nomic Embed v1.5** (`nomic-ai/nomic-embed-text-v1.5`):
- 768 dimensions, no matryoshka support → no dimension validation issues
- 2048 max context (not 8192 as initially assumed)
- `NomicBertModel` supported in vLLM 3.3.0 (was rejected in 3.2.5)
- Requires `--trust-remote-code` vLLM arg
- Still requires the vLLM matryoshka patch (see below) because LlamaStack sends
  `dimensions=768` and vLLM rejects ANY `dimensions` param for non-matryoshka models

### vLLM Matryoshka Validation Patch (Init Container)

vLLM 0.13 (RHOAI 3.3.0) rejects the `dimensions` parameter for ANY model that
doesn't declare matryoshka support. Since LlamaStack 0.5.0 always sends `dimensions`
during embedding ingestion, ALL embedding models are affected — not just Snowflake.

**Fix:** Init container in the embed-model Deployment copies the vllm package to a
writable `emptyDir` volume and patches `pooling_params.py`:

```python
# Original (line 168):
if self.dimensions is not None and model_config is not None:

# Patched:
if False:  # patched: skip matryoshka validation
```

This allows both matryoshka and non-matryoshka models to accept explicit `dimensions`
without error. The patch is in `charts/embed-model/templates/inferenceservice.yaml`.

**Note:** `--override-pooler-config` is NOT available in vLLM 0.13 (added in 0.14+).
There is no env var equivalent either.

### Milvus v2.5.27 Required for BM25 Sparse Index

LlamaStack 0.5.0's remote Milvus provider creates collections with a sparse vector
field using `SPARSE_INVERTED_INDEX` with `metric_type="BM25"`. This BM25 metric type
was introduced in Milvus 2.5.0. Milvus 2.4.9 does not support it and fails with:
```
MilvusException: only IP is the supported metric type for sparse index
```

### nomic-embed Model Registration Resets on Restart

The built-in config's `vllm-embedding` provider auto-discovers models from the vLLM
endpoint. It registers `nomic-embed` as `type=llm` by default. After every LlamaStack
restart, the model must be manually re-registered as `type=embedding` with
`embedding_dimension: 768`:

```bash
curl -X DELETE http://localhost:8321/v1/models/vllm-embedding/nomic-embed
curl -X POST http://localhost:8321/v1/models -H "Content-Type: application/json" \
  -d '{"model_id":"nomic-embed","provider_id":"vllm-embedding","provider_model_id":"nomic-embed","model_type":"embedding","metadata":{"embedding_dimension":768}}'
```

**Permanent fix options:**
- Custom ConfigMap with explicit model registration in `registered_resources`
- Init container that patches the config to include the correct model type
- Post-start script that calls the registration API after LlamaStack is healthy

### LlamaStack v0.5.0 Streaming + Instructions Fixed

The v0.3.5 bugs are confirmed fixed in v0.5.0:
- **Streaming + file_search:** v0.3.5 skipped file_search results when streaming was
  enabled. v0.5.0 correctly injects retrieved context during streaming.
- **Instructions + file_search:** v0.3.5 skipped file_search when `instructions` were
  provided. v0.5.0 handles both correctly.

### Citation Token Leaking (LlamaStack Design Issue)

LlamaStack injects `<|file-{id}|>` annotation tokens into the model prompt alongside
retrieved document chunks. The model (especially Llama 3.1 8B) frequently echoes these
tokens in its output, along with:
- `"Cite sources:"` blocks listing file IDs
- Raw tool call JSON: `{"name": "knowledge_search", "parameters": {...}}`

**This is a LlamaStack v0.5.0 design issue** — not configurable via any env var, RHOAI
setting, or vLLM parameter. The annotation injection is hardcoded in the `inline::rag-runtime`
provider and the Responses API handler.

**Mitigations applied:**
1. Instructions tell the model not to add citations (partially effective)
2. Application-level streaming filter strips `<|file-...|>` tokens, citation blocks,
   and leaked `knowledge_search` tool call JSON

**Permanent fix:** Upgrade to LlamaStack 0.7.x+ when available in RHOAI.

### Postgres Connection Issue on LlamaStack Restart

**Observed behavior:** When LlamaStack pods are restarted (e.g., by ArgoCD sync),
they frequently fail to connect to Postgres with `RuntimeError: Could not connect
to PostgreSQL database server`, even though the Postgres pod is running and healthy.

**Workaround:** Restart the Postgres deployment. LlamaStack then connects successfully
on its next restart attempt.

**Root cause:** Unknown — possibly a connection pool exhaustion issue or a DNS
resolution timing problem during pod startup.

### Multi-Model Support Limitation

The built-in RHOAI config supports only ONE inference model via `VLLM_URL`. Adding
additional models (e.g., Qwen alongside Llama) requires a second `remote::vllm` provider,
which cannot be created via the API (POST /v1/providers returns 405 Method Not Allowed).

**Options evaluated:**
1. **Init container config patching** — failed due to `yaml.safe_load/dump` destroying
   the `${env.VAR}` substitution syntax in the built-in config
2. **Direct KServe service access** — failed due to cross-namespace NetworkPolicy/Istio
   blocking (KServe workload services return empty reply from other namespaces)
3. **Custom ConfigMap** — the approach used on the old cluster (v0.3.5). Requires
   replicating the full LlamaStack config (~345 lines) but with env var references
   preserved. This is the proven path forward.

**Current state:** Single model (Llama 3.1 8B) via MaaS gateway. Multi-model is parked.
