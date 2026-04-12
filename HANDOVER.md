# MaaS 2.0 — Session Handover (April 12, 2026)

## Repo & Branch
- **Repo:** `github.com/anatsheh84/MaaS-2.0`
- **Local clone:** `/Users/aelnatsh/Lab/latest-MaaS/MaaS-2.0`
- **Branch:** `rhoai-3.4`

## What This Session Accomplished

### RHOAI 3.4 Platform
- Upgraded from 3.3.0 to **3.4.0-ea.1** (beta channel, Automatic install plan)
- Added **LeaderWorkerSet operator** to GitOps (`charts/openshift-ai/templates/leaderworkerset-operator.yaml`)
- Added **tier-to-group-mapping** ConfigMap to GitOps (`charts/openshift-ai/templates/tier-to-group-mapping.yaml`)
- Both deploy at wave 5 (after RHOAI operator, before models)

### Embedding Model — Nomic Embed v1.5
- **Switched from** Snowflake Arctic Embed L v2.0 **to** Nomic Embed Text v1.5
- **Why:** Snowflake has `matryoshka_dimensions: [256]` in HuggingFace config. vLLM 0.13 rejects explicit `dimensions` param for matryoshka models unless the value is in the allowed list. LlamaStack 0.5.0 sends `dimensions` during ingestion but NOT during search — causing dimension mismatch in Milvus.
- Nomic: 768 dimensions, 2048 max context, `--trust-remote-code` required
- **vLLM matryoshka patch:** Init container copies vllm package to writable emptyDir, patches `pooling_params.py` to skip dimension validation (`if False:` replacing the check). Required because LlamaStack sends `dimensions=768` and vLLM rejects ANY dimensions param for non-matryoshka models.

### rag-central — Centralized LlamaStack Deployment
- **All components GitOps-managed** via `charts/rag-central/` (ArgoCD Application at wave 8)
- PostgreSQL 16.11 (10Gi PVC, Secret preserved via `lookup()`)
- Milvus **v2.5.27** (upgraded from v2.4.9 — required for BM25 sparse index)
- Milvus etcd v3.5.16 + MinIO
- LlamaStack 0.5.0 (stateless, env-var driven, no custom ConfigMap)
- S3 bucket for file storage

### Notebook API Fixes
- **Model discovery:** Fixed for LlamaStack 0.5.0 API format (`custom_metadata.model_type` + `id` field)
- **Full model ID:** UI sends `vllm-inference/llama-3-1-8b-instruct-fp8` (not bare name)
- **Stale model mapping removed:** Old `maas-{model}/{model}` prefix deleted
- **Real streaming:** Replaced fake 3-word-chunk simulation with actual SSE streaming from LlamaStack Responses API (v0.3.5 file_search bug fixed in v0.5.0)
- **Instructions re-enabled:** v0.3.5 bug (file_search skipped when instructions present) fixed in v0.5.0
- **Citation/tool-call filtering:** Strips `<|file-...|>` tokens, `Cite sources:` blocks, and leaked `knowledge_search` tool call JSON from streamed output

### Models
- All models set to `stopped: true` in values (deployed but no pods)
- Phi-4: `enabled: false` (not deployed at all)
- `ignoreDifferences` for stop annotation so dashboard can start/stop without ArgoCD reverting
- **On fresh cluster: must manually start Llama from RHOAI dashboard for RAG to work**

### configure.sh Updates
- **Step 5f added:** Creates `rag-central` namespace, `llamastack-vllm-token` (enterprise SA token), S3 bucket, and `llamastack-s3` secret
- **Note:** vLLM token creation will fail on first run because the enterprise-tier SA doesn't exist until ArgoCD deploys the MaaS gateway (wave 6). Script warns and provides manual commands.

---

## Post-Deployment Manual Steps (Fresh Cluster)

After `./setup/configure.sh` completes and ArgoCD finishes syncing:

### 1. Create vLLM enterprise token (if Step 5f skipped it)
```bash
TOKEN=$(oc create token llamastack-internal \
  -n maas-default-gateway-tier-enterprise \
  --audience=maas-default-gateway-sa \
  --duration=8760h)
oc create secret generic llamastack-vllm-token \
  --from-literal=token="$TOKEN" \
  -n rag-central
```

### 2. Start at least one LLM model
From the RHOAI dashboard → Model Serving → Start `llama-3-1-8b-instruct-fp8`
Or via CLI:
```bash
oc annotate llminferenceservice llama-3-1-8b-instruct-fp8 -n llm \
  serving.kserve.io/stop- --overwrite
```

### 3. Register nomic-embed as embedding model
After LlamaStack pod is running (may need Postgres restart workaround first):
```bash
# Delete the auto-registered llm entry
oc exec deployment/llamastack -n rag-central -- \
  curl -s -X DELETE http://localhost:8321/v1/models/vllm-embedding/nomic-embed

# Register as embedding with correct dimensions
oc exec deployment/llamastack -n rag-central -- \
  curl -s -X POST http://localhost:8321/v1/models \
  -H "Content-Type: application/json" \
  -d '{"model_id":"nomic-embed","provider_id":"vllm-embedding","provider_model_id":"nomic-embed","model_type":"embedding","metadata":{"embedding_dimension":768}}'
```

### 4. Postgres restart workaround
If LlamaStack keeps crashing with `Could not connect to PostgreSQL database server`:
```bash
oc rollout restart deployment/llamastack-postgres -n rag-central
```
LlamaStack will connect on its next restart attempt.

### 5. Trigger notebook-api/notebook-ui builds
```bash
oc start-build notebook-api -n maas-rag
oc start-build notebook-ui -n maas-rag
```
Wait for builds to complete, then restart deployments if ArgoCD hasn't already.

---

## Known Issues / Limitations

| Issue | Status | Workaround |
|---|---|---|
| nomic-embed resets to `type=llm` on LlamaStack restart | Manual fix needed each restart | Run the registration commands above |
| Postgres connection fails on LlamaStack restart | Intermittent | Restart Postgres deployment |
| Citation tokens `<\|file-...\|>` leak in responses | Mitigated | App-level filter + instructions (LlamaStack design issue, fixed in 0.7.x) |
| Multi-model dropdown (Qwen) | Parked | Needs custom ConfigMap with multiple vllm providers (same approach as old cluster) |
| Duplicate Milvus (maas-rag + rag-central) | Low priority | `charts/milvus/` deploys unused Milvus to maas-rag namespace |
| RHOAI dashboard redirect loop | RHOAI 3.4-ea.1 bug | Clear cookies + incognito, or use OpenShift console directly |

---

## Key File Locations

| File | Purpose |
|---|---|
| `charts/rag-central/` | Centralized LlamaStack Helm chart (Postgres, Milvus, LlamaStack) |
| `charts/embed-model/values.yaml` | Nomic Embed v1.5 config |
| `charts/embed-model/templates/inferenceservice.yaml` | Init container vLLM matryoshka patch |
| `charts/models/values.yaml` | Model definitions with enabled/stopped flags |
| `charts/notebook-api/app/main.py` | Chat endpoint with streaming + citation filter |
| `charts/notebook-api/app/llamastack_client.py` | LlamaStack client with instructions |
| `charts/notebook-api/values-llmaas.yaml` | notebook-api config overrides |
| `charts/openshift-ai/templates/leaderworkerset-operator.yaml` | LWS operator + CR |
| `charts/openshift-ai/templates/tier-to-group-mapping.yaml` | Tier mapping ConfigMap |
| `charts/install-operators/values-llmaas.yaml` | RHOAI operator: beta/3.4.0-ea.1/Automatic |
| `knowledge/llamastack.md` | LlamaStack deployment knowledge base |
| `setup/configure.sh` | Cluster bootstrap script (Steps 1-7 + 5f for rag-central) |
