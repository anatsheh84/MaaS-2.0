# CLAUDE.md — MaaS 2.0 Project Brief

> This file is read automatically by Claude Code at every session start.
> It gives Claude Code full project context without re-briefing.
> Keep it updated as the project evolves.

---

## Who You Are Working With

**Ziko** — Principal Solution Architect, Red Hat KSA.
Strategic customers: PIF, SEC, Riyadh Air.
Working style: wants to understand each step, not just have things done.
Validate live before committing. Explain what you are doing and why.

---

## Project Overview

**MaaS 2.0** is a fully GitOps-driven Models-as-a-Service platform on OpenShift AI.
It demonstrates and deploys multi-tenant, GPU-accelerated LLM serving to enterprise customers.
Everything is managed via ArgoCD (app-of-apps pattern). All changes go through this Git repo.

**Active use cases:**
1. MaaS API gateway — multi-tenant LLM serving with Kuadrant rate limiting and auth
2. LlamaStack playground — per-user agentic workspaces with MCP tool integration
3. NotebookLM-equivalent (IN PLANNING) — per-notebook RAG over user documents using LlamaStack

---

## Repository

| Item | Value |
|---|---|
| **GitHub repo** | `https://github.com/anatsheh84/MaaS-2.0` |
| **Branch** | `main` |
| **Local clone** | `/Users/aelnatsh/Lab/MaaS` |

---

## Cluster (current sandbox)

| Item | Value |
|---|---|
| **API** | `https://api.cluster-66k9k.66k9k.sandbox5291.opentlc.com:6443` |
| **Apps domain** | `apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| **kubeadmin password** | `UXhDv-Ac2r7-8ghvp-p2TEg` |
| **ArgoCD URL** | `https://openshift-gitops-server-openshift-gitops.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| **ArgoCD admin password** | `Gha7EnSYkDmU49b1CtAgPWey5JucopXs` |
| **AWS region/AZ** | `us-east-2` / `us-east-2b` |

### Authenticate to cluster
```bash
oc login https://api.cluster-66k9k.66k9k.sandbox5291.opentlc.com:6443 \
  -u admin -p NDcxOTE3 --insecure-skip-tls-verify
```

Use `admin` (NOT `kubeadmin`) for RHOAI operations — kubeadmin is tier-free only.

---

## Repo Structure

```
MaaS-2.0/
├── bootstrap/                  # ArgoCD app-of-apps Helm chart
│   ├── templates/applications/ # One ArgoCD Application per service
│   └── values.yaml             # Cluster-agnostic defaults (template only)
├── charts/                     # One Helm chart per service
│   ├── cert-manager/
│   ├── cluster-certificates/
│   ├── cluster-monitoring/
│   ├── devspaces/
│   ├── grafana/                # 3 dashboards: maas-lab, SDAIA Phase 1, SDAIA Principles
│   ├── install-operators/
│   ├── installplan-approver/
│   ├── keycloak/               # Disabled (keycloak.enabled: false in bootstrap)
│   ├── keycloak-instance/      # Disabled
│   ├── kubernetes-mcp-server/
│   ├── litemaas/               # LiteLLM proxy in litemaas namespace
│   ├── llama-stack-instance/   # LlamaStack distribution per user workspace
│   ├── machinesets/            # GPU worker MachineSet definitions
│   ├── model-registry/         # maas-registry + self-contained Postgres
│   ├── models/                 # All LLMInferenceService CRs
│   ├── models-as-a-service/    # MaaS API gateway + Kuadrant policies
│   ├── nvidia-gpu-enablement/  # NFD + GPU Operator
│   ├── openshift-ai/           # RHOAI DataScienceCluster + HardwareProfiles
│   ├── rhcl-operator/          # RHCL / Kuadrant operator
│   ├── slack-mcp/
│   └── workspace/
├── setup/
│   └── configure.sh            # 7-step bootstrap script
├── docs/
├── HANDOVER.md                 # Full session state — read this for deep context
└── CLAUDE.md                   # This file
```

---

## Active Models

All 4 models run on a single g6e.12xlarge (4× NVIDIA L40S, 46GB each).
Each model gets exclusive access to one GPU. No MIG, no time-slicing.
Runtime: `registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.5`

| Model key | Image | GPU mem | Context | Tool parser |
|---|---|---|---|---|
| `qwen3-4b-instruct` | `quay.io/jharmison/models:qwen--qwen3-4b-instruct-2507-modelcar` | 0.95 | 131072 | hermes |
| `llama-3-1-8b-instruct-fp8` | `registry.redhat.io/rhelai1/modelcar-llama-3-1-8b-instruct-fp8-dynamic:1.5` | 0.95 | 131072 | llama3_json |
| `mistral-small-24b-fp8` | `registry.redhat.io/rhelai1/modelcar-mistral-small-3-1-24b-instruct-2503-fp8-dynamic:1.5` | 0.95 | 65536 | mistral |
| `phi-4-instruct-w8a8` | `registry.redhat.io/rhelai1/modelcar-phi-4-quantized-w8a8:1.5` | 0.95 | 16384 | hermes |

**Model notes:**
- Mistral FP8: do NOT add `--tokenizer-mode=mistral` — it uses HuggingFace tokenizer format
- Phi-4: 16K context only — smallest window; max_tokens capped at 16384
- HardwareProfile API is `v1` (not `v1alpha1`)
- Annotation key: `opendatahub.io/hardware-profile-name`

### MaaS gateway endpoints
Base URL: `https://maas.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com`
```
/llm/qwen3-4b-instruct/v1
/llm/llama-3-1-8b-instruct-fp8/v1
/llm/mistral-small-24b-fp8/v1
/llm/phi-4-instruct-w8a8/v1
```

---

## Users and Rate Limiting

| User | Password | Groups | Rate limit |
|---|---|---|---|
| `user1` | `191457` | `tier-premium-users` | 20 req/2min |
| `user2` | `191457` | `tier-premium-users` | 20 req/2min |
| `admin` | `471917` | `tier-enterprise-users`, `rhods-admins` | **Unlimited** |

Enterprise tier has NO ceiling (removed to prevent LlamaStack agentic loops from hitting limit).

---

## LlamaStack

Three LlamaStackDistribution instances, one per user workspace namespace.

| Namespace | User | VLLM_MAX_TOKENS | Model provider |
|---|---|---|---|
| `wksp-user1` | user1 | 8192 | qwen3-4b only |
| `wksp-user2` | user2 | 8192 | qwen3-4b only |
| `admin-wkspc` | admin | 4096 (dashboard-managed) | llama-3-1-8b only |

**Pending:** All three instances need multi-model support. The fix is to update
`charts/llama-stack-instance/templates/configmap.yaml` to loop over a models list
so each instance has all 4 models registered as inference providers.

**admin-wkspc VLLM_MAX_TOKENS:** The admin workspace is managed by RHOAI GenAI Studio dashboard.
Patching via `oc` is reverted within seconds. Change it through the RHOAI Dashboard UI only.

---

## LiteMaaS

LiteLLM proxy in `litemaas` namespace. All 4 models exposed with enterprise-tier SA tokens.

| LiteLLM model name | MaaS backend |
|---|---|
| `Qwen-local-4B` | `/llm/qwen3-4b-instruct/v1` |
| `Llama-local-8B` | `/llm/llama-3-1-8b-instruct-fp8/v1` |
| `Mistral-local-24B` | `/llm/mistral-small-24b-fp8/v1` |
| `Phi-4-local` | `/llm/phi-4-instruct-w8a8/v1` |

LiteMaaS URL: `https://litemaas-litemaas.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com`
LiteLLM master key: `sk-b0c603570f0ad9ab801879bb88a821de55b0c247d53cc57d`

---

## Model Registry

Deployed via `charts/model-registry/`. ArgoCD app: `model-registry`.

- Namespace: `rhoai-model-registries`
- Instance name: `maas-registry`
- Postgres: `maas-registry-postgres` Deployment + 10Gi gp3-csi PVC
- DB password: `maas-registry-db-2024`
- REST API port: 8080
- Status: `Available=True`, ArgoCD shows OutOfSync cosmetically (operator annotation drift — known issue)

---

## ArgoCD Sync Patterns

### Standard refresh trigger
```bash
oc annotate application <app-name> -n openshift-gitops \
  argocd.argoproj.io/refresh=hard --overwrite
sleep 20
oc get application <app-name> -n openshift-gitops \
  -o jsonpath='{.status.sync.status} {.status.health.status}'
```

### Force sync with prune
```bash
oc patch application <app-name> -n openshift-gitops --type merge \
  -p '{"operation":{"initiatedBy":{"username":"admin"},"sync":{"revision":"HEAD","prune":true}}}'
```

### After any git push (always run both)
```bash
cd /Users/aelnatsh/Lab/MaaS && git pull origin main
oc annotate application <app-name> -n openshift-gitops \
  argocd.argoproj.io/refresh=hard --overwrite
```

### Suspend selfHeal on a child app
Patch the **bootstrap** app first, then the child — the app-of-apps re-renders within seconds
if bootstrap selfHeal remains active.

---

## Critical Engineering Rules

These are hard-won lessons from this project. Never violate them.

1. **GitOps first** — all changes live in Git. Live cluster edits are for validation only before committing.
2. **Never create GitOps deployments for auto-installed operator dependencies** — Service Mesh 3, Authorino, Limitador, DevWorkspace are all auto-installed; don't manage them via ArgoCD.
3. **ServerSideApply risk on PVC-owning resources** — enabling `ServerSideApply=true` can trigger PVC deletion/recreation cycles. Requires manual intervention (delete mounting pod, recreate PVC). Avoid unless necessary.
4. **Stale `kubectl.kubernetes.io/last-applied-configuration` annotations** — if they contain status blocks, they cause permanent OutOfSync. Always clean to spec-only content.
5. **`gpu_memory_utilization=0.95` on all models** — each pod gets exclusive GPU access on g6e.12xlarge.
6. **Validate before commit** — always check `git status` and revert uncommitted local changes before starting new work. Run `git pull origin main` first.
7. **GitHub multi-file commits** — use `github:push_files` (no SHA pre-fetch needed). Single-file updates require `github:get_file_contents` first to get current SHA.
8. **Pod failure triage order** — `oc get events --sort-by='.lastTimestamp'` → `oc describe pod` → `oc logs --previous`
9. **Self-contained charts** — all upstream dependencies have been consolidated into this repo. No external Helm repo dependencies.
10. **`RespectIgnoreDifferences=true`** — required on machinesets to allow GUI scaling without ArgoCD reverting.

---

## Next Work Items (In Priority Order)

### 1. LlamaStack multi-model ConfigMap
File: `charts/llama-stack-instance/templates/configmap.yaml`
Goal: Loop over a models list so all 4 models are registered as inference providers per instance.
Current state: Each instance only has ONE provider (Qwen or Llama). Tool calls route incorrectly for Mistral/Phi.
Approach: Suspend ArgoCD selfHeal → edit ConfigMap live → validate all model+MCP combinations → commit as Helm template.

### 2. NotebookLM feature (RAG Notebooks)
**New charts needed:**
- `charts/milvus/` — vector store, shared instance, per-notebook collection isolation
- `charts/embed-model/` — nomic-embed-text-v1.5 or bge-m3 as KServe InferenceService
- `charts/notebook-api/` — FastAPI service: notebook CRUD, file upload to ODF S3, Tekton PipelineRun trigger
- `charts/docling-serve/` — Docling document parser REST service (replaces heavy Python parsing libs)
- `charts/notebook-ui/` — PatternFly 6 frontend (fork of rh-aiservices-bu/rh-kb-chat)

**Architecture decisions made:**
- LlamaStack native RAG (not LangChain) — use `memory_banks.register()` + `agents.create_turn()` with MemoryToolConfig
- Milvus collection naming: `notebook_{user_id}_{notebook_id}` for per-user isolation
- Ingest pipeline: FastAPI background task for demo; Tekton PipelineRun for production
- Auth: Kuadrant JWT flows through to collection scoping — each request only accesses its own collection
- Reference implementation: `rh-aiservices-bu/rh-kb-chat` on GitHub (study the Milvus retriever, config.json multi-model pattern, Tekton pipeline, async streaming pattern)
- TTS (audio overview): deferred — Kokoro TTS InferenceService added after core RAG demo works

**Demo scope (3-4 days):** Upload document → ask questions → cited answers from LlamaStack RAG agent.
Multi-tenant isolation, Tekton pipeline, TTS are post-demo.

### 3. admin-wkspc VLLM_MAX_TOKENS
Set to 16384 via RHOAI Dashboard UI (not patchable via oc — dashboard controller reverts it).

---

## Key File Reference

| File | What it controls |
|---|---|
| `charts/models/values.yaml` | All 4 model definitions |
| `charts/openshift-ai/values-llmaas.yaml` | HardwareProfiles, DataScienceCluster, dashboardConfig |
| `charts/model-registry/values-llmaas.yaml` | Model Registry instance |
| `charts/model-registry/templates/` | Postgres Deployment + ModelRegistry CR |
| `charts/llama-stack-instance/templates/configmap.yaml` | LlamaStack run.yaml (needs multi-model update) |
| `charts/llama-stack-instance/values.yaml` | deployer.domain, model.name defaults |
| `charts/models-as-a-service/` | MaaS API gateway, Kuadrant AuthPolicy, RateLimitPolicy |
| `charts/grafana/templates/` | All dashboard definitions |
| `bootstrap/templates/applications/` | All ArgoCD Application/ApplicationSet CRs |
| `bootstrap/values.yaml` | Cluster-agnostic defaults template |
| `setup/configure.sh` | 7-step bootstrap script |
| `HANDOVER.md` | Full deep-dive session history and known issues |

---

## How Claude Code and Claude.ai Desktop Divide Work

**Claude Code (you — this tool):**
- Write all new code, charts, Python services
- Edit Helm templates, values.yaml files
- Generate boilerplate (Dockerfiles, requirements.txt, FastAPI stubs)
- Hold full repo in 1M context — reason about cross-chart dependencies
- Commit and push changes to GitHub

**Claude.ai Desktop (companion session — has Desktop Commander + oc):**
- Validate everything on the live cluster after pushes
- Run `oc` commands, check pod status, watch ArgoCD sync
- Debug what breaks after deploy
- Architecture decisions and planning
- Review what you produce before it gets committed

**Workflow:**
```
Claude Code writes → git push → Claude.ai validates on cluster → iterate
```

Never make live cluster changes without a corresponding Git commit to follow.
