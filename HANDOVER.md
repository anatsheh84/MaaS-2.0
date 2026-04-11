# HANDOVER — Session April 12, 2026

## Cluster
- **ID:** cluster-szwjm
- **RHOAI:** 3.4.0-ea.1 (upgraded from 3.3.0 this session)
- **Branch:** `rhoai-3.4`
- **Local clone:** `/Users/aelnatsh/Lab/latest-MaaS/MaaS-2.0`

## What Was Done This Session

### RHOAI 3.4 Upgrade
- Upgraded from 3.3.0 to 3.4.0-ea.1 (beta channel, no `replaces` chain — required
  delete/recreate subscription approach)
- Changed `installPlanApproval` to `Automatic` for fresh cluster deployments
- Installed LeaderWorkerSet operator (required by RHOAI 3.3+ for LLMInferenceService)
  - Channel: `stable-v1.0`, namespace: `openshift-lws-operator`, specific namespace mode
  - Must create `LeaderWorkerSetOperator` CR after operator install

### Models
- All models set to `stopped: true` in git (deployed but no pods)
- Added `serving.kserve.io/stop` annotation support to model template
- Added `ignoreDifferences` for stop annotation so dashboard can start/stop without ArgoCD reverting
- Phi-4 set to `enabled: false` (not deployed at all)
- Snowflake embed: removed `--task=embed` (invalid in vLLM 3.3.0, auto-detected)

### Removed from GitOps
- User workspaces (workspace.yaml ApplicationSet)
- LlamaStack instances (llama-stack-instance.yaml ApplicationSet)
- Slack MCP application
- configure.sh step 5f (llamastack-vllm-token provisioning for workspaces)

### Fixed Issues
- `tier-to-group-mapping` ConfigMap copied to `redhat-ods-applications` to satisfy
  RHOAI 3.4 validating webhook on LLMInferenceService mutations
- `notebook-api` tierNamespaces set to `[]` in both values.yaml and values-llmaas.yaml
- `notebook-ui` build: added `--chown=1001:0` to COPY commands for UBI Node.js image
- `notebook-ui` and `notebook-api` routes: injected `deployer.domain` from bootstrap
  template via inline helm values (was hardcoded to old cluster domain)
- `notebook-api` llamastackUrl updated to `http://llamastack.rag-central.svc.cluster.local:8321`

### rag-central Deployment (NEW)
Built and deployed a centralized LlamaStack in `rag-central` namespace:

| Component | Status | Notes |
|---|---|---|
| PostgreSQL 16.11 | ✅ Running | 10Gi PVC, Secret with lookup() |
| Milvus v2.4.9 standalone | ✅ Running | 100Gi data + 100Gi MinIO + 5Gi etcd |
| S3 bucket | ✅ Validated | `llamastack-files-cluster-szwjm-zckdr` in us-east-2 |
| LlamaStack 0.5.0 | ✅ Running | Stateless, env-var driven, no ConfigMap |

**Key discovery:** The RHOAI LlamaStack image has a built-in parameterized config —
no custom ConfigMap needed. Everything driven by env vars (POSTGRES_HOST, MILVUS_ENDPOINT,
ENABLE_S3, VLLM_URL, etc.). See `knowledge/llamastack.md` for full details.

**Validated:** LlamaStack → MaaS gateway → Llama 3.1 8B inference working.

## Current Cluster State

### Nodes
- 3 non-GPU workers (Ready)
- 1 GPU node g6e.12xlarge (Ready, replaced during session — old 3 nodes went NotReady)
- 1 control plane (Ready)

### Running Pods (llm namespace)
- `llama-3-1-8b-instruct-fp8` — Running, Ready
- `snowflake-embed` — Running
- Other models: Stopped (deployed but no pods)

### ArgoCD Applications
All on `rhoai-3.4` branch. Key apps:
- `models` — Synced, ignoreDifferences for stop annotation
- `notebook-api` — Synced, Healthy, pointing to rag-central LlamaStack
- `notebook-ui` — Synced, route on correct domain
- `rag-central` — Not yet managed by ArgoCD (chart committed but bootstrap
  Application not yet synced — needs bootstrap refresh)

## Pending / Next Steps

1. **Sync rag-central ArgoCD Application** — refresh bootstrap to pick up
   the new `rag-central.yaml` Application template. Note: the manually-created
   resources in rag-central will conflict with ArgoCD's desired state.
   May need to delete manually-created resources first, or add ignoreDifferences
   for the postgres Secret (to preserve the existing password).

2. **Add S3 + vllm-token creation to configure.sh** — the `llamastack-s3` and
   `llamastack-vllm-token` secrets in rag-central are cluster-specific and not
   in git. Need configure.sh steps to create them on fresh clusters.

3. **Test notebook end-to-end** — notebook UI → notebook-api → LlamaStack →
   Llama 3.1 + Milvus + S3. The model IDs may need adjustment in notebook-api
   code (built-in config uses `vllm-inference/llama-3-1-8b-instruct-fp8` format).

4. **Multi-model support** — current setup only supports one LLM via VLLM_URL.
   For multiple models, need custom ConfigMap approach (see knowledge/llamastack.md
   Pitfall #6).

5. **HPA for LlamaStack** — parked. LlamaStack is fully stateless now, so HPA
   is a simple addition when needed.

## Key Commits This Session
- `e5bd38a` — ignoreDifferences for stop annotation
- `99cacdd` — inject deployer.domain into notebook apps
- `c82e258` — deploy models in stopped state
- `023c5c4` — disable all models (temporary, then reverted to stopped)
- `9b0b186` — rag-central Helm chart
- `51c6ec1` — LlamaStack knowledge base
