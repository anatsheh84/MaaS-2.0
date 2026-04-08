# MaaS 2.0 — Handover Document
**Date:** 2026-04-08  
**Cluster:** `https://api.cluster-ttpwl.ttpwl.sandbox962.opentlc.com:6443`  
**Repo:** `https://github.com/anatsheh84/MaaS-2.0` | Local: `/Users/aelnatsh/Lab/MaaS`  
**Latest commit:** `f138e41`  
**Apps domain:** `apps.cluster-ttpwl.ttpwl.sandbox962.opentlc.com`

---

## Cluster Credentials

```bash
oc login https://api.cluster-ttpwl.ttpwl.sandbox962.opentlc.com:6443 \
  --username=admin --password=<admin-password>
```

Users: `user1` / `user2` / `admin` / `kube:admin`  
Identity provider: `htpasswd-maas`

---

## Platform Overview

GitOps-driven MaaS 2.0 platform on OpenShift AI. All components managed by ArgoCD
(`openshift-gitops`). Bootstrap app at `setup/bootstrap.yaml`.

**Active models** (namespace: `llm`, g6e.12xlarge, 4× NVIDIA L40S):
- `qwen3-4b-instruct`
- `llama-3-1-8b-instruct-fp8`
- `mistral-small-24b-fp8`
- `phi-4-instruct-w8a8`

**MaaS gateway:** `https://maas.apps.cluster-ttpwl.ttpwl.sandbox962.opentlc.com`  
**Gateway auth:** Kuadrant `kubernetesTokenReview`, audience `maas-default-gateway-sa`

**Tier rate limits** (Kuadrant `RateLimitPolicy`, counter per `auth.identity.userid`):
- `free` → 5 req / 2 min
- `premium` → 20 req / 2 min
- `enterprise` → no limit

**Tier SA namespaces:**
- `maas-default-gateway-tier-free` → `kube-admin-81378af5`
- `maas-default-gateway-tier-premium` → `user1-b3daa77b`
- user2 has NO tier SA yet (falls back to shared free token — see Pending Items)

---

## RAG Notebook App (namespace: `maas-rag`)

**UI:** `https://notebook.apps.cluster-ttpwl.ttpwl.sandbox962.opentlc.com`  
**Auth:** OpenShift OAuth via `oauth-proxy` sidecar (2/2 containers in notebook-ui pod)  
**notebook-api:** Internal only — no public Route. Reachable via nginx proxy at `/api/`

### Architecture

```
Browser → oauth-proxy:4180 (OpenShift login redirect)
               ↓ X-Forwarded-User: username
          nginx:8080 (serves React UI, proxies /api/ to notebook-api)
               ↓
          notebook-api:8000 (FastAPI)
               ↓ K8s API: find user's tier SA → TokenRequest
          MaaS gateway (per-user token, tier-enforced)
               ↓
          LlamaStack (wksp-user1) → vLLM inference
          Milvus (vector store, gp3-csi PVCs)
```

### Key Components

| Resource | Namespace | Notes |
|---|---|---|
| `notebook-api` Deployment | `maas-rag` | SA: `notebook-api`, FastAPI |
| `notebook-ui` Deployment | `maas-rag` | 2 containers: oauth-proxy + nginx |
| `milvus` Deployment | `maas-rag` | Vector store, gp3-csi PVCs |
| `notebook-ui-proxy` Secret | `maas-rag` | oauth-proxy cookie secret |
| `maas-gateway-token` Secret | `maas-rag` | Shared fallback free-tier token |
| `notebook-api` SA | `maas-rag` | RBAC: list LLMInferenceServices in `llm`; list/token SAs in tier namespaces |

### Per-User Token Flow

```
X-Forwarded-User: user1
  → _get_cached_user_token("user1")
  → scan tier namespaces for SA starting with "user1-"
  → found: user1-b3daa77b in maas-default-gateway-tier-premium
  → TokenRequest API → 1h token (audience: maas-default-gateway-sa)
  → cached in memory with TTL refresh at expiry-5min
  → used for MaaS gateway calls → premium rate limits applied
```

If no tier SA found → falls back to shared `maas-gateway-token` (free tier, 5 req/2min shared).

### notebook-api Endpoints

| Endpoint | Notes |
|---|---|
| `GET /models` | Lists active LLMInferenceServices from K8s API |
| `POST /notebooks` | Creates notebook + LlamaStack vector store |
| `POST /notebooks/{id}/documents` | Upload + ingest (embed timeout: 600s for large files) |
| `POST /notebooks/{id}/chat` | SSE streaming chat, uses per-user token |
| `GET /user-token/{username}` | Returns tier SA token — internal use only |

---


## LlamaStack Instances

| Namespace | Service | Port | User |
|---|---|---|---|
| `wksp-user1` | `lsd-genai-playground-service` | 8321 | user1 |
| `wksp-user2` | `lsd-genai-playground-service` | 8321 | user2 |
| `mydsproject` | `lsd-genai-playground-service` | 8321 | admin |

**notebook-api uses:** `lsd-genai-playground-service.wksp-user1.svc.cluster.local:8321`

---

## 🔴 CRITICAL PENDING ISSUE — LlamaStack VLLM Token

### Problem
`VLLM_API_TOKEN_1: fake` is hardcoded in the `LlamaStackDistribution` CR.
LlamaStack refreshes its model list from the MaaS gateway every 5 minutes using this
token. When `fake` hits Kuadrant's `kubernetesTokenReview` it returns **401**.
After several failed refreshes, inference requests also fail with 401.

**This is why chat breaks for all users after ~5 minutes of a fresh LlamaStack pod.**

### What Was Done
- ✅ `llamastack-vllm-token` secret created in all 3 wksp namespaces with real 8760h SA token
- ✅ Chart updated: `charts/llama-stack-instance/templates/llamastack-distribution.yaml`
  now uses `valueFrom.secretKeyRef` pointing to `llamastack-vllm-token`
- ✅ ArgoCD synced — `LlamaStackDistribution` CR now has `valueFrom.secretKeyRef`
- ❌ **The `LlamaStackDistribution` operator does NOT propagate `valueFrom` to the Deployment**
  It only renders `value: fake` into the Deployment spec regardless of what the CR says

### Root Cause of Operator Limitation
The operator (`registry.redhat.io/rhoai/odh-llama-stack-k8s-operator-rhel9`) reads
`env[].value` fields from the CR and writes them verbatim to the Deployment.
It does not pass through `valueFrom.secretKeyRef` — this is a bug/limitation in the
operator version `0.4.0` on this cluster.

### Resolution Options (pick one)

**Option A — Patch Deployment directly + add ignoreDifferences to ArgoCD**
```bash
# Get real token
TOKEN=$(oc get secret llamastack-vllm-token -n wksp-user1 \
  -o jsonpath='{.data.token}' | base64 -d)

# Patch deployment directly
oc set env deployment/lsd-genai-playground \
  VLLM_API_TOKEN_1="$TOKEN" -n wksp-user1

# Also patch wksp-user2 and mydsproject
```
Then add `ignoreDifferences` to the `llama-stack-instance-user1` ArgoCD app so it
doesn't revert the env var on next sync.

**Option B — Patch the LlamaStackDistribution CR with literal token value**
```bash
TOKEN=$(oc get secret llamastack-vllm-token -n wksp-user1 \
  -o jsonpath='{.data.token}' | base64 -d)

oc patch llamastackdistribution lsd-genai-playground -n wksp-user1 \
  --type=merge \
  -p "{\"spec\":{\"server\":{\"containerSpec\":{\"env\":[
    {\"name\":\"VLLM_TLS_VERIFY\",\"value\":\"false\"},
    {\"name\":\"MILVUS_DB_PATH\",\"value\":\"~/.llama/milvus.db\"},
    {\"name\":\"FMS_ORCHESTRATOR_URL\",\"value\":\"http://localhost\"},
    {\"name\":\"VLLM_MAX_TOKENS\",\"value\":\"8192\"},
    {\"name\":\"VLLM_API_TOKEN_1\",\"value\":\"$TOKEN\"},
    {\"name\":\"LLAMA_STACK_CONFIG_DIR\",\"value\":\"/opt/app-root/src/.llama/distributions/rh/\"}
  ]}}}}"
```
**This is the recommended option** — the operator WILL propagate a literal `value`
to the Deployment. Token is 8760h (1 year) so refresh is not an issue.
Repeat for `wksp-user2` and `mydsproject`.

**Verification after fix:**
```bash
# Confirm token is real in pod env
oc exec -n wksp-user1 deployment/lsd-genai-playground -- \
  sh -c 'echo "token: ${VLLM_API_TOKEN_1:0:20}..."'
# Should NOT print "token: fake..."

# Confirm no more 401 errors
oc logs -n wksp-user1 deployment/lsd-genai-playground --tail=10 | \
  grep -E "401|Error|Model refresh"
# Should be empty
```

---


## Other Pending Items

### 1. user2 Has No Tier SA
user2 is in `tier-premium-users` group but has no SA in any tier namespace.
Chat works but falls back to shared free-tier token (5 req/2min shared).

**Fix:** Create SA via maas-api onboarding flow, or manually:
```bash
HASH=$(echo -n "user2" | sha256sum | cut -c1-8)
oc create sa user2-${HASH} -n maas-default-gateway-tier-premium
oc label sa user2-${HASH} -n maas-default-gateway-tier-premium \
  app.kubernetes.io/component=token-issuer \
  app.kubernetes.io/part-of=maas-api \
  maas.opendatahub.io/instance=maas-default-gateway \
  maas.opendatahub.io/tier=premium
```

### 2. kube:admin Username Normalization Bug
`kube:admin` has a colon but the tier SA is named `kube-admin-81378af5` (hyphen).
The SA lookup uses `sa_name.startswith(f"{username}-")` which fails because
`"kube-admin-...".startswith("kube:admin-")` is False.

**Fix in `_get_user_maas_token` in `charts/notebook-api/app/main.py`:**
```python
# Normalize username — OpenShift uses "kube:admin" but SA names use hyphens
normalized_username = username.replace(":", "-")
user_sa = next(
    (sa for sa in items if sa["metadata"]["name"].startswith(f"{normalized_username}-")),
    None,
)
```

### 3. Notebook State is In-Memory Only
`notebooks: dict[str, dict] = {}` in `main.py` — wiped on pod restart.
Vector stores persist in Milvus PVCs but notebook metadata is lost.
Users must create a new notebook after any pod restart.

**Fix:** Add SQLite or Redis persistence for the `notebooks` dict and `ingest_status`.

### 4. UI Has No 404 Error Handling
When a notebook ID no longer exists (pod restart), chat silently fails.
The UI needs to detect HTTP 404 responses and prompt user to create a new notebook.

### 5. mydsproject LlamaStack Not in ArgoCD
`mydsproject` has a LlamaStack instance but no ArgoCD app manages it.
The `llamastack-vllm-token` secret was created manually.
Consider adding a `workspace-mydsproject` ArgoCD app mirroring `workspace-user1/2`.

---

## Key Files

| File | Purpose |
|---|---|
| `charts/notebook-api/app/main.py` | FastAPI app, per-user token, token cache, model discovery |
| `charts/notebook-api/app/llamastack_client.py` | Vector store, RAG retrieval, streaming chat |
| `charts/notebook-api/app/ingest.py` | PDF/DOCX text extraction, LlamaStack file upload + embed |
| `charts/notebook-api/app/config.py` | Settings: llamastackUrl, maasBaseUrl, maasToken, tierNamespaces |
| `charts/notebook-api/templates/rbac.yaml` | SA + Roles for LLMInferenceService list + tier SA tokens |
| `charts/notebook-ui/templates/deployment.yaml` | oauth-proxy sidecar + nginx |
| `charts/notebook-ui/templates/sa.yaml` | SA with oauth-redirectreference annotation |
| `charts/llama-stack-instance/templates/llamastack-distribution.yaml` | LlamaStack CR (valueFrom not propagated by operator — see issue above) |
| `setup/configure.sh` | Full deployment automation script |

---

## Standard Operations

### Rebuild notebook-api
```bash
oc start-build notebook-api -n maas-rag
# Watch: oc get builds -n maas-rag -w
oc rollout restart deployment/notebook-api -n maas-rag
```

### Rebuild notebook-ui
```bash
oc start-build notebook-ui -n maas-rag
oc rollout restart deployment/notebook-ui -n maas-rag
```

### ArgoCD hard refresh + force sync
```bash
oc annotate application <app-name> -n openshift-gitops \
  argocd.argoproj.io/refresh=hard --overwrite
sleep 20
oc patch application <app-name> -n openshift-gitops --type merge \
  -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"revision":"HEAD","prune":true}}}'
```

### Suspend ArgoCD selfHeal (before live cluster edits)
```bash
oc patch application <app-name> -n openshift-gitops --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":false,"prune":true}}}}'
# ... make changes ...
# Re-enable:
oc patch application <app-name> -n openshift-gitops --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true,"prune":true}}}}'
```

### Check notebook-api logs
```bash
oc logs -n maas-rag deployment/notebook-api -f | \
  grep -E "per-user token|fallback|Ingest|ERROR|401|429"
```

### Check oauth-proxy auth
```bash
oc logs -n maas-rag -l app=notebook-ui -c oauth-proxy | \
  grep "authentication complete"
```

---

## Git Workflow — ALWAYS Local First

```bash
# Always pull before editing
cd /Users/aelnatsh/Lab/MaaS
git pull origin main

# Edit files locally
# Commit and push
git add <files>
git commit -m "feat/fix: description"
git push origin main
```

**NEVER use GitHub API (`github:push_files` / `github:create_or_update_file`) directly
for code changes — always go through local git to avoid conflicts.**

