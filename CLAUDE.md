# MaaS 2.0 — Claude Code Project Brief

This file is read automatically by Claude Code at the start of every session.
It provides full project context so you never need to re-explain the setup.

---

## Who you are working with

**Ziko** — Principal Solution Architect, Red Hat KSA.  
Strategic customers: PIF, SEC, Riyadh Air.  
Goal: build and demo enterprise-grade AI infrastructure on OpenShift AI.

**Your role in Claude Code**: write code, edit Helm charts, generate boilerplate, refactor templates, and reason over the full codebase. You work on the local clone. Validation on the live cluster is done separately via Claude.ai Desktop (Desktop Commander + `oc` CLI).

---

## Repository

| Item | Value |
|---|---|
| Repo | `https://github.com/anatsheh84/MaaS-2.0` |
| Local clone | `/Users/aelnatsh/Lab/MaaS` |
| Primary branch | `main` |
| Commit style | `type(scope): description` (conventional commits) |

---

## Cluster (current sandbox)

| Item | Value |
|---|---|
| API | `https://api.cluster-66k9k.66k9k.sandbox5291.opentlc.com:6443` |
| Apps domain | `apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| ArgoCD URL | `https://openshift-gitops-server-openshift-gitops.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| Platform | AWS us-east-2, RHPDS sandbox |

**Note**: cluster coordinates change between RHPDS allocations. Always check `bootstrap/values.yaml` for the current domain values.

---

## Repo Layout

```
MaaS-2.0/
├── bootstrap/                    # ArgoCD app-of-apps (the entry point)
│   ├── Chart.yaml
│   ├── values.yaml               # Cluster-agnostic defaults (committed)
│   └── templates/
│       └── applications/         # One ArgoCD Application per chart
├── charts/
│   ├── cert-manager/             # Let's Encrypt + Route53 DNS-01
│   ├── cluster-certificates/     # Wildcard cert for *.apps domain
│   ├── cluster-monitoring/       # OpenShift monitoring stack config
│   ├── devspaces/                # Red Hat Dev Spaces
│   ├── grafana/                  # Grafana + dashboards (SDAIA, MaaS lab)
│   ├── install-operators/        # Operator subscriptions
│   ├── installplan-approver/     # Auto-approves InstallPlans
│   ├── keycloak/                 # RHSSO operator (keycloak.enabled=false currently)
│   ├── keycloak-instance/        # Keycloak realm + clients
│   ├── kubernetes-mcp-server/    # Kubernetes MCP tool for LlamaStack agents
│   ├── litemaas/                 # LiteLLM proxy frontend (litemaas namespace)
│   ├── llama-stack-instance/     # LlamaStack playground per user
│   ├── machinesets/              # GPU worker MachineSet definitions
│   ├── model-registry/           # ModelRegistry CR + Postgres (rhoai-model-registries ns)
│   ├── models/                   # LLMInferenceService CRs for all models
│   ├── models-as-a-service/      # Kuadrant gateway, AuthPolicy, RateLimitPolicy
│   ├── nvidia-gpu-enablement/    # NFD + GPU Operator
│   ├── openshift-ai/             # RHOAI DataScienceCluster, HardwareProfiles
│   ├── rhcl-operator/            # RHCL / Kuadrant operator
│   ├── slack-mcp/                # Slack MCP server
│   └── workspace/                # Namespace + RBAC scaffolding
├── docs/                         # Architecture diagrams, decision records
├── setup/
│   └── configure.sh              # 7-step cluster bootstrap (run once per cluster)
├── HANDOVER.md                   # Full live state snapshot (detailed)
└── CLAUDE.md                     # This file
```

---

## Platform Architecture Summary

MaaS 2.0 is a **fully GitOps-driven Models-as-a-Service platform** deployed on OpenShift AI.
All platform state lives in this repo. ArgoCD reconciles the cluster to match git.

### Request flow
```
Client → OpenShift Route → Kuadrant Gateway → AuthPolicy (JWT/SAR) →
RateLimitPolicy (per tier) → vLLM InferenceService (KServe) → GPU
```

### Tiers
| Tier | Rate limit | Token mechanism |
|---|---|---|
| free | 5 req / 2 min | Any authenticated user |
| premium | 20 req / 2 min | SA token, group `tier-premium-users` |
| enterprise | Unlimited | SA token, group `tier-enterprise-users` |

### Users
| User | Password | Groups |
|---|---|---|
| user1 | `191457` | tier-premium-users |
| user2 | `191457` | tier-premium-users |
| admin | `471917` | tier-enterprise-users, rhods-admins |

**Always use `admin` (not `kubeadmin`) for RHOAI playground access.**

---

## Active Models

All deployed via `charts/models/values.yaml`. Runtime: `registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.5`
HardwareProfile: `nvidia-l40s-single-gpu` (1× L40S per pod, `gpu_memory_utilization=0.95`)

| Name | Model ID | Context | Tool parser | Notes |
|---|---|---|---|---|
| `qwen3-4b-instruct` | qwen3-4b | 131072 | hermes | Fast, good for agents |
| `llama-3-1-8b-instruct-fp8-dynamic` | llama-3.1-8b-instruct | 131072 | llama3_json | Primary RAG model |
| `mistral-small-24b-fp8` | mistral-small-24b | 65536 | mistral | Best reasoning |
| `phi-4-instruct-w8a8` | phi-4 | 16384 | hermes | Smallest context window |

### Critical model notes
- **Mistral FP8**: do NOT use `--tokenizer-mode=mistral` — uses HuggingFace tokenizer format
- **Phi-4**: 16K context only — always cap `max_tokens` at 16384
- **All models**: `gpu_memory_utilization=0.95` — pods get exclusive GPU access on g6e.12xlarge
- **HardwareProfile annotation**: `opendatahub.io/hardware-profile-name` (not `alpha.kubeflow.org/...`)
- **HardwareProfile API version**: `v1` (not `v1alpha1`)

### Model endpoints (gateway)
```
https://maas-default-gateway-maas.apps.<domain>/llm/<model-name>/v1
```

---

## LlamaStack

| Instance | Namespace | User | Max tokens | Status |
|---|---|---|---|---|
| admin-wkspc | redhat-ods-applications | admin | 4096 (dashboard-managed) | Running |
| wksp-user1 | redhat-ods-applications | user1 | 8192 | Running |
| wksp-user2 | redhat-ods-applications | user2 | 8192 | Running |

### Known LlamaStack issues
- All three instances currently have **only one inference provider** (single model)
- **Pending**: update `charts/llama-stack-instance/templates/configmap.yaml` to loop over all 4 models
- `admin-wkspc` is dashboard-managed — do not patch the CR or Deployment directly; use RHOAI UI
- Agentic tool calls multiply inference requests — enterprise rate limit was removed to prevent admin hitting ceiling

### LlamaStack ConfigMap fix (next pending task)
The `run.yaml` in the ConfigMap must register each model as a separate inference provider:
```yaml
models:
  - model_id: llama3.1-8b
    provider_id: vllm-llama
  - model_id: mistral-small-24b
    provider_id: vllm-mistral
  ...
```
Test-first: suspend ArgoCD selfHeal → edit live → validate all model+MCP combos → commit as template.

---

## Key Component Details

### ArgoCD (openshift-gitops)
- Bootstrap app: `bootstrap` — the app-of-apps that spawns all child apps
- All child apps point to `charts/<name>/` with `targetRevision: main`
- **selfHeal is ON** by default — to test a live edit, always suspend selfHeal on the child app first
- To patch bootstrap selfHeal: patch the `bootstrap` app **first** (not a child app) — otherwise it re-renders child specs within seconds

### ArgoCD sync commands
```bash
# Hard refresh (re-fetch from git without syncing)
oc annotate application <name> -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite

# Force sync with prune
oc patch application <name> -n openshift-gitops --type merge \
  -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"revision":"HEAD","prune":true}}}'
```

### Model Registry (`maas-registry`)
- Namespace: `rhoai-model-registries`
- Chart: `charts/model-registry/`
- Status: `Available=True` — **fully functional**
- ArgoCD shows OutOfSync (cosmetic) — operator writes annotation drift after every sync
- ignoreDifferences covers: `/spec/rest/image`, `/spec/rest/resources`, `/spec/grpc/image`,
  `/spec/postgres/image`, `/status`, `/metadata/annotations/kubectl.kubernetes.io~1last-applied-configuration`

### Kuadrant / MaaS Gateway
- Gateway: `maas-default-gateway` in `maas` namespace
- AuthPolicy: JWT validation via `maas-default-gateway-authpolicy`
- RateLimitPolicy: `gateway-rate-limits` — 3 tiers, enterprise has no limit
- All `RateLimitPolicy` resources must live in the same namespace as the Gateway

### LiteMaaS
- Namespace: `litemaas`
- LiteLLM master key: `sk-b0c603570f0ad9ab801879bb88a821de55b0c247d53cc57d`
- All 4 models proxied using enterprise SA tokens (1-year TTL)
- URL: `https://litemaas-litemaas.apps.<domain>`

---

## GitOps Workflow (enforce always)

```
1. Make changes in local clone (/Users/aelnatsh/Lab/MaaS)
2. git add + git commit + git push origin main
3. git pull origin main (keep local in sync)
4. Annotate ArgoCD app with refresh=hard
5. Wait ~20s, check sync status
6. Validate on cluster with oc commands
7. If broken: fix in git, repeat
```

**Never make permanent cluster changes without a corresponding git commit.**  
**Live edits are for validation only — always commit the fix before moving on.**

### GitHub multi-file commits
- Use `github:push_files` (no SHA pre-fetch needed for new files)
- Single-file updates to existing files: use `github:get_file_contents` first to get SHA, then `github:create_or_update_file`

### After every git push
```bash
cd /Users/aelnatsh/Lab/MaaS && git pull origin main
oc annotate application <name> -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite
```

---

## Pod Failure Triage Sequence

```bash
oc get events -n <namespace> --sort-by='.lastTimestamp' | tail -20
oc describe pod <pod> -n <namespace>
oc logs <pod> -n <namespace> --previous
```

---

## Known Pitfalls (learn from these)

| Pitfall | What happens | Fix |
|---|---|---|
| `ServerSideApply=true` on PVC resources | Can trigger PVC deletion/recreation cycle | Keep SSA off for Postgres PVCs; use `ignoreDifferences` instead |
| Stale `kubectl.kubernetes.io/last-applied-configuration` with status blocks | Permanent OutOfSync loop | Clean annotation to spec-only content, add `ignoreDifferences` |
| Patching child app selfHeal without patching bootstrap first | Bootstrap re-renders child spec within seconds | Always patch bootstrap app first |
| `--tokenizer-mode=mistral` on FP8 Mistral | Pod CrashLoopBackOff | Remove the flag — FP8 uses HuggingFace tokenizer |
| `v1alpha1` HardwareProfile API | Resource not found | Use `v1` |
| Pre-creating operator-managed resources | Conflict / stuck reconcile | Let operators create their own resources |
| `gpu_memory_utilization` < 0.95 on g6e.12xlarge | GPU underutilised | Keep at 0.95 for all models |

---

## In-Progress Features

### NotebookLM-equivalent (MaaS Notebooks)
Building a NotebookLM-style RAG application on top of MaaS 2.0.
Decisions already made:
- **Orchestration**: LlamaStack agents (NOT LangChain) — Red Hat supported
- **Vector store**: Milvus — shared instance, per-notebook collection isolation
- **Document parsing**: Docling Serve (containerized REST API)
- **Embedding model**: nomic-embed-text-v1.5 as a KServe InferenceService
- **Ingest trigger**: Tekton PipelineRun on user document upload
- **Backend**: FastAPI service (`charts/notebook-api/`)
- **Frontend**: Fork of rh-kb-chat PatternFly 6 UI + notebook management + upload
- **Auth**: Kuadrant JWT flows through to Milvus collection scoping per user
- **TTS (phase 2)**: Kokoro TTS InferenceService for audio overview feature
- **Reference project**: `https://github.com/rh-aiservices-bu/rh-kb-chat`
  - Borrow: Milvus score-threshold retriever pattern, Tekton ingest pipeline structure,
    async streaming pattern (asyncio.Queue bridge), multi-model config structure
  - Build new: per-user notebook isolation, upload API, LlamaStack RAG agent wiring,
    notebook CRUD, Helm chart (their deploy is plain YAML, no ArgoCD)

### Charts to be created
```
charts/milvus/              # Vector store — shared instance, HNSW index
charts/embed-model/         # nomic-embed-text-v1.5 InferenceService
charts/docling-serve/       # Document parsing service
charts/notebook-api/        # FastAPI: notebook CRUD, upload, ingest trigger, RAG chat
charts/notebook-ui/         # PatternFly 6 frontend
```

---

## Coding Standards

- **Python**: FastAPI for services, async throughout, Pydantic v2 models
- **Helm**: all charts self-contained (no external repo dependencies)
- **Helm values**: cluster-specific values always in `values.yaml` with empty defaults; never hardcode cluster URLs in templates
- **Namespaces**: defined in chart, not assumed to pre-exist
- **Secrets**: never committed — always referenced as `existingSecret` or mounted from cluster Secrets
- **Resource limits**: always set `requests` and `limits` in Deployments
- **ArgoCD apps**: always include `ignoreDifferences` for operator-managed fields
- **Sync waves**: use `argocd.argoproj.io/sync-wave` annotations for ordering; operators before CRs before workloads

---

## How Claude Code and Claude.ai Desktop divide the work

```
Claude Code (you, in this terminal)     Claude.ai Desktop (companion session)
──────────────────────────────────      ──────────────────────────────────────
Write all Python, YAML, Helm            Run oc commands on live cluster
Edit templates and values files         Check ArgoCD sync status
Generate boilerplate fast               Read pod logs, debug failures
Refactor across the full codebase       Validate deployments post-push
Commit and push to GitHub               Architecture decisions, planning
```

When Claude Code produces code or charts, the Desktop session validates them on the cluster.
Always write code here, verify there.
