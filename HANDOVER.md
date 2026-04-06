# MaaS 2.0 GitOps Deployment — Handover Document

## Context & Role
You are continuing an active deployment session for Ziko, a Principal Solution Architect at Red Hat KSA.
The platform is **MaaS 2.0** — a fully GitOps-driven Models-as-a-Service platform on OpenShift AI.
All actions use Desktop Commander (`start_process`) with `oc` CLI. Git changes go through the local clone.
Ziko runs commands personally and reports output — your role is analysis, fix design, and repo commits.

---

## Environment

| Item | Value |
|---|---|
| **Repo** | `https://github.com/anatsheh84/MaaS-2.0` (branch: `main`) |
| **Local clone** | `/Users/aelnatsh/Lab/MaaS` |
| **Cluster API** | `https://api.cluster-66k9k.66k9k.sandbox5291.opentlc.com:6443` |
| **kubeadmin password** | `UXhDv-Ac2r7-8ghvp-p2TEg` |
| **Cluster domain** | `apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| **AWS AZ** | `us-east-2b` |
| **Hosted Zone ID** | `Z09995911COTKS4VI7RXG` |
| **ArgoCD URL** | `https://openshift-gitops-server-openshift-gitops.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com` |
| **ArgoCD admin password** | `Gha7EnSYkDmU49b1CtAgPWey5JucopXs` |

---

## Platform Architecture

The MaaS stack deploys via ArgoCD app-of-apps (bootstrap chart). Components:
- **MachineSets** — GPU worker nodes on AWS
- **cert-manager** — TLS certificates via Let's Encrypt + Route53
- **NVIDIA GPU enablement** — NFD + GPU Operator
- **OpenShift AI (RHOAI) 3.2** — DataScienceCluster + KServe for model serving
- **Models** — LLMInferenceService CRs per model, served via vLLM
- **MaaS API** — Multi-tenant API gateway with Kuadrant auth (TokenReview + SubjectAccessReview)
- **RHCL/Kuadrant** — Rate limiting + AuthPolicy per tier (free/premium/enterprise)
- **LlamaStack** — Per-user playground instances (wksp-user1, wksp-user2, admin-wkspc)
- **Model Registry** — `maas-registry` in `rhoai-model-registries` namespace with self-contained Postgres
- **Grafana** — 3 dashboards: maas-lab, SDAIA Phase 1, SDAIA Principles View
- **LiteMaaS** — LiteLLM-based frontend in `litemaas` namespace
- **DevSpaces, Slack MCP, Kubernetes MCP** — Supporting services

### Key Principles (enforce these always)
1. **All fixes must be in Git** — no manual cluster changes without a repo commit
2. **Never touch what an operator owns** — don't pre-create operator-managed resources
3. **`gpu_memory_utilization=0.95` for all models** — each pod gets exclusive GPU on g6e.12xlarge
4. **`RespectIgnoreDifferences=true` on machinesets** — allows GUI scaling without ArgoCD reverting

---

## GPU Nodes (Current State)

| MachineSet | Instance | GPUs | Replicas | Status |
|---|---|---|---|---|
| `worker-gpu-g6e-2xlarge-us-east-2b` | g6e.2xlarge | 1× L40S (46GB) | 0 | **Scaled down** |
| `worker-gpu-g6e-12xlarge-us-east-2b` | g6e.12xlarge | 4× L40S (184GB) | 1 | **Running** |

Each model pod gets **exclusive access to one GPU** — no MIG, no time-slicing.

---

## Models Currently Deployed

All models in `charts/models/values.yaml`. All use `nvidia-l40s-single-gpu` HardwareProfile.
Runtime image: `registry.redhat.io/rhaiis/vllm-cuda-rhel9:3.2.5`

| Name | URI | GPU mem | Context | Tool parser | Status |
|---|---|---|---|---|---|
| `qwen3-4b-instruct` | `oci://quay.io/jharmison/models:qwen--qwen3-4b-instruct-2507-modelcar` | 0.95 | 131072 | hermes | ✅ Ready |
| `llama-3-1-8b-instruct-fp8` | `oci://registry.redhat.io/rhelai1/modelcar-llama-3-1-8b-instruct-fp8-dynamic:1.5` | 0.95 | 131072 | llama3_json | ✅ Ready |
| `mistral-small-24b-fp8` | `oci://registry.redhat.io/rhelai1/modelcar-mistral-small-3-1-24b-instruct-2503-fp8-dynamic:1.5` | 0.95 | 65536 | mistral | ✅ Ready |
| `phi-4-instruct-w8a8` | `oci://registry.redhat.io/rhelai1/modelcar-phi-4-quantized-w8a8:1.5` | 0.95 | 16384 | hermes | ✅ Ready |

### Model Notes
- Mistral FP8: do NOT add `--tokenizer-mode=mistral` — uses HuggingFace tokenizer, not Mistral-native
- Phi-4: 16K context window — smallest of the 4; max_tokens capped at 16384
- All 4 models serve via MaaS gateway at `http://maas.apps.cluster-66k9k.../llm/<name>/v1`

---

## ArgoCD Application Status (last known good)

| Application | Sync | Health | Notes |
|---|---|---|---|
| `bootstrap` | Synced | Healthy | App-of-apps |
| `openshift-ai` | Synced | Healthy | |
| `models` | Synced | Healthy | 4 models |
| `model-registry` | OutOfSync | Healthy | CR spec matches; diff is cosmetic (operator annotation drift). CR is `Available=True`, registry is fully functional. |
| `grafana` | Synced | Healthy | 3 dashboards |
| `machinesets` | OutOfSync | Healthy | Cosmetic — replica ignoreDifferences |
| `slack-mcp` | Synced | Progressing | Needs `slack-mcp-token` secret manually |

---

## HTPasswd Users

| User | Password (base64) | Groups | Rate limit |
|---|---|---|---|
| `user1` | `MTkxNDU3` | `tier-premium-users` | 20 req/2min |
| `user2` | `MTkxNDU3` | `tier-premium-users` | 20 req/2min |
| `admin` | `NDcxOTE3` | `tier-enterprise-users`, `rhods-admins` | **Unlimited** |

**Important**: Use `admin` (not `kubeadmin`) for RHOAI playground — kubeadmin is tier-free only.

---

## Rate Limiting (Kuadrant `gateway-rate-limits` RateLimitPolicy)

| Tier | Limit | Users |
|---|---|---|
| `free` | 5 req / 2min | `system:authenticated` default |
| `premium` | 20 req / 2min | user1, user2 |
| `enterprise` | **No limit** | admin |

Enterprise limit was removed (was 50/2min) to prevent LlamaStack agentic tool calls from hitting the ceiling.

---

## LlamaStack Playground

| Namespace | User | VLLM_MAX_TOKENS | Model provider | Status |
|---|---|---|---|---|
| `wksp-user1` | user1 | 8192 | Single: qwen3-4b only | Running |
| `wksp-user2` | user2 | 8192 | Single: qwen3-4b only | Running |
| `admin-wkspc` | admin | 4096 (dashboard-managed, fights patches) | Single: llama-3-1-8b only | Running |

### Pending: LlamaStack Multi-Model Support
All three instances currently have only ONE inference provider. The fix is to update
`charts/llama-stack-instance/templates/configmap.yaml` to loop over the models list.
See the agreed approach in `charts/llama-stack-instance/` values files.

### admin-wkspc VLLM_MAX_TOKENS issue
The `admin-wkspc` LlamaStackDistribution is managed by the RHOAI GenAI Studio dashboard (not GitOps).
Patching the CR or Deployment is reverted by the dashboard controller within seconds.
To change `VLLM_MAX_TOKENS` for admin: use the RHOAI Dashboard UI → GenAI Studio → edit the playground.
Recommended value: **16384** (safe for all models except phi-4 with long history).

---

## Model Registry (`maas-registry`)

Deployed via `charts/model-registry/` chart, ArgoCD app `model-registry`.
- Namespace: `rhoai-model-registries`
- Postgres: `maas-registry-postgres` Deployment + 10Gi gp3-csi PVC
- REST API: `maas-registry` pod, port 8080, route enabled
- DB password: `maas-registry-db-2024` (in `maas-registry-postgres` Secret)
- Status: `Available=True`, ArgoCD shows OutOfSync cosmetically due to operator annotation drift
  - `ignoreDifferences` covers `/spec/rest/image`, `/spec/rest/resources`, `/spec/grpc/image`,
    `/spec/postgres/image`, `/status`, `/metadata/annotations/kubectl.kubernetes.io~1last-applied-configuration`

### Known ArgoCD OutOfSync Behaviour
The ModelRegistry operator mutates the CR after every ArgoCD sync, writing a `last-applied-configuration`
annotation that includes operator-injected status fields. This creates a permanent diff cycle.
The registry is **fully functional** regardless. This is a known RHOAI 3.2 / ArgoCD interaction issue.

---

## LiteMaaS

Deployed in `litemaas` namespace. LiteLLM proxy in front of all 4 MaaS models.

| Model name in LiteLLM | MaaS endpoint | Provider |
|---|---|---|
| `Qwen-local-4B` | `.../llm/qwen3-4b-instruct/v1` | openai |
| `Llama-local-8B` | `.../llm/llama-3-1-8b-instruct-fp8/v1` | openai |
| `Mistral-local-24B` | `.../llm/mistral-small-24b-fp8/v1` | openai |
| `Phi-4-local` | `.../llm/phi-4-instruct-w8a8/v1` | openai |

All use enterprise-tier SA tokens (1-year, audience=`maas-default-gateway-sa`).
LiteLLM master key: `sk-b0c603570f0ad9ab801879bb88a821de55b0c247d53cc57d`
LiteMaaS URL: `https://litemaas-litemaas.apps.cluster-66k9k.66k9k.sandbox5291.opentlc.com`

---

## Hardware Profile

`HardwareProfile/nvidia-l40s-single-gpu` in `redhat-ods-applications`:
- 1× `nvidia.com/gpu` (L40S)
- CPU: default 4, max 6 | Memory: default 32Gi, max 48Gi
- nodeSelector: `nvidia.com/gpu.present: "true"`
- toleration: `nvidia.com/gpu=l40-gpu:NoSchedule`
- Annotation key: `opendatahub.io/hardware-profile-name` (NOT `alpha.kubeflow.org/...`)

---

## Key File Paths

| File | Purpose |
|---|---|
| `charts/models/values.yaml` | Model list — 4 models including phi-4 |
| `charts/openshift-ai/values-llmaas.yaml` | HardwareProfiles, DataScienceCluster, dashboardConfig |
| `charts/model-registry/values-llmaas.yaml` | Model Registry instance definition |
| `charts/model-registry/templates/` | Postgres + ModelRegistry CR templates |
| `charts/llama-stack-instance/templates/configmap.yaml` | LlamaStack run.yaml template (single model — needs multi-model update) |
| `charts/llama-stack-instance/values.yaml` | Default values (deployer.domain, model.name) |
| `charts/grafana/templates/dashboard-sdaia-phase1.yaml` | SDAIA Governance Phase 1 dashboard |
| `charts/grafana/templates/dashboard-sdaia-principles-view.yaml` | SDAIA Principles compliance dashboard |
| `bootstrap/templates/applications/` | All ArgoCD Application/ApplicationSet definitions |
| `bootstrap/values.yaml` | Cluster-agnostic defaults |
| `setup/configure.sh` | 7-step cluster bootstrap script |

---

## Pending Work

1. **LlamaStack multi-model configmap** — update `charts/llama-stack-instance/templates/configmap.yaml`
   to loop over models list (see original HANDOVER for test ConfigMap structure and approach)
2. **admin-wkspc VLLM_MAX_TOKENS** — set to 16384 via RHOAI Dashboard UI (not patchable via oc)
3. **model-registry OutOfSync** — cosmetic only; investigate if newer RHOAI or ArgoCD version resolves

---

## Recent Commit History

```
7a04fb8 fix(grafana): escape Helm template delimiters in SDAIA dashboard legendFormat fields
1babeae feat(grafana): add SDAIA AI Ethics Principles compliance view dashboard
743734e feat(grafana): add SDAIA AI Governance Phase 1 dashboard (Principles 5 & 7)
a22cd8f fix(model-registry): remove ServerSideApply — use ignoreDifferences for last-applied-configuration
95142b5 fix(model-registry): use ServerSideApply=true to prevent last-applied-configuration drift
44400e2 fix(model-registry): remove sync-wave from CR + ignore annotation drift
c3c3313 fix(model-registry): add sslMode=disable to CR + ignoreDifferences for operator-injected fields
b6f0e63 fix(model-registry): self-contained chart — own Postgres + correct ModelRegistry CR spec
f5aa21b feat(model-registry): dedicated chart + ArgoCD Application targeting rhoai-model-registries
ff7c55e feat(openshift-ai): add Model Registry via GitOps — data-driven CR template
c3b152f fix(rate-limit): remove enterprise tier limit — admin/enterprise users are now unlimited
f1b85bf feat(models): add phi-4-instruct-w8a8 as fourth model on 4th L40S GPU
```
