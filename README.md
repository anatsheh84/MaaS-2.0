# MaaS 2.0

GitOps bootstrap for a **Model-as-a-Service (MaaS)** platform on OpenShift — multi-tenant LLM serving with self-service access via [LiteMaaS](https://github.com/anatsheh84/lite-maas).

---

## Stack Architecture

![Stack Architecture](docs/stack-architecture.svg)

---

## Quick Start

### Prerequisites

- `oc` CLI installed and in PATH, logged in as cluster-admin
- `htpasswd` installed (`httpd-tools` on RHEL/Fedora, `apache2-utils` on Debian/Ubuntu)
- Run from the root of the cloned repo

That's it. No container runtime, no registry credentials, no cloud CLI needed.
All values are auto-discovered from the live cluster. Container images for the
RAG Notebook service are built directly on OpenShift using its built-in build
service — nothing runs locally.

### Deploy

Log in once, then run with zero arguments — everything else is auto-discovered:

```bash
# 1. Log in to the cluster
oc login https://api.<cluster>:6443 \
  -u kubeadmin -p <PASSWORD> \
  --insecure-skip-tls-verify

# 2. Run — no arguments needed
./setup/configure.sh
```

The script auto-discovers all values from the live cluster (API URL, AWS credentials,
Hosted Zone ID, infraID, AMI, AZ, region) and handles the full deployment:

| Step | What happens |
|------|-------------|
| 1 | Verifies active `oc` session with cluster-admin privileges |
| 2 | Auto-discovers all cluster values via `oc` |
| 3 | Writes `bootstrap/values.local.yaml` — gitignored, never committed |
| 4 | Creates `cert-manager-aws-creds` secret |
| 5 | Installs OpenShift GitOps operator |
| 5b | Creates HTPasswd IDP (`user1`, `user2`, `admin`) |
| 5c | Generates LiteMaaS secrets with `openssl rand`, creates `OAuthClient` |
| 5d | Creates BuildConfigs for `notebook-api` and `notebook-ui` — images built and stored in the OpenShift internal registry, no external registry needed |
| 6 | Deploys ArgoCD bootstrap Application |
| 7 | Patches `helm.valuesObject` with real values — no git commit needed |

> Nothing sensitive is ever committed to git. All secrets are generated at
> deploy-time and stored only on the cluster.

### After deployment

Monitor ArgoCD sync progress:

```bash
oc get applications -n openshift-gitops -w
```

One manual step remains — the Slack bot token:

```bash
oc create secret generic slack-mcp-token -n lls-demo \
  --from-literal=slack-bot-token=<SLACK_BOT_TOKEN>
```


---

## Platform Overview

### Repository Layout

```
bootstrap/                   ← Root Helm chart (app-of-apps)
  values.yaml                ← Template — cluster values injected at runtime
  templates/
    applications/            ← ArgoCD Application CRs (one per component)
    extra-resources/         ← Shared cluster-scoped resources

charts/                      ← Local Helm charts per component
  machinesets/               ← AWS MachineSets (workers + GPU)
  cert-manager/              ← cert-manager Operator
  keycloak/                  ← RHBK Operator
  keycloak-instance/         ← Keycloak DB, CR, realm, route, OAuth
  litemaas/                  ← LiteMaaS portal
  models-as-a-service/       ← MaaS API + Kuadrant Gateway
  milvus/                    ← Milvus vector store (standalone + etcd + MinIO)
  embed-model/               ← Embedding model InferenceService (CPU)
  notebook-api/              ← RAG Notebook backend (FastAPI)
  notebook-ui/               ← RAG Notebook frontend (PatternFly 6)
  ...

setup/                       ← One-time bootstrap (run once, not GitOps)
  configure.sh               ← Auto-configure + deploy (zero arguments)
```

### Infrastructure

| Parameter | Value |
|---|---|
| Cloud | AWS `us-east-2b` |
| OpenShift | 4.20 |

| MachineSet | Instance | vCPU | RAM | GPUs | Replicas |
|---|---|---|---|---|---|
| Workers | `m6a.4xlarge` | 16 | 64 GB | — | 5 |
| GPU (active) | `g6e.12xlarge` | 48 | 192 GB | **4× NVIDIA L40S** | 1 |
| GPU (standby) | `g6e.2xlarge` | 8 | 32 GB | 1× NVIDIA L40S | 0 |

### ArgoCD Sync Waves

| Wave | Application | Description |
|------|-------------|-------------|
| 0 | `machinesets` | AWS MachineSets |
| 1 | `cert-manager` | cert-manager Operator |
| 2 | `cluster-certificates` | Let's Encrypt wildcard cert |
| 2 | `keycloak` | RHBK Operator |
| 3 | `keycloak-instance` | Keycloak DB + realm + OAuth |
| 4 | `nvidia-gpu-enablement` | NFD + NVIDIA GPU Operator |
| 4 | `openshift-ai-operator` | RHOAI Operator |
| 4 | `rhcl-operator` | Kuadrant (RHCL) |
| 4 | `grafana` | Grafana + dashboards |
| 5 | `openshift-ai` | DataScienceCluster operand |
| 6 | `models` | LLMInferenceServices |
| 6 | `models-as-a-service` | MaaS API + Kuadrant Gateway |
| 7 | `milvus` | Vector store for RAG (etcd + MinIO + Milvus) |
| 7 | `llama-stack-instance` | Per-user LlamaStack distributions |
| 8 | `embed-model` | Embedding model (nomic-embed, CPU) |
| 8 | `litemaas` | LiteMaaS portal |
| 9 | `notebook-api` | RAG Notebook backend |
| 9 | `notebook-ui` | RAG Notebook frontend |


---

## RAG Notebooks

A NotebookLM-equivalent feature built natively on LlamaStack and OpenShift AI.
Upload documents, ask questions, get cited answers — all running on your cluster.

### Architecture

```
Browser → notebook-ui (PatternFly 6)
               ↓  REST + SSE
          notebook-api (FastAPI)
               ↓              ↓
        LlamaStack RAG     Milvus (vector store)
        (memory banks)     (per-notebook collections)
               ↓
        LLM models via MaaS gateway
```

### How images are built

`configure.sh` creates two `BuildConfig` resources in the `maas-rag` namespace.
OpenShift clones the source from GitHub, builds the images on cluster using its
native build service, and stores them in the **internal image registry** — no
container runtime, no external registry, and no credentials are needed on the
operator's machine.

```bash
# Trigger a rebuild after a code change (optional — selfHeal handles it)
oc start-build notebook-api -n maas-rag
oc start-build notebook-ui  -n maas-rag

# Watch build progress
oc get builds -n maas-rag -w
```

### Access

| Service | URL |
|---|---|
| Notebook UI | `https://notebook.apps.<cluster-domain>` |
| Notebook API | `https://notebook-api.apps.<cluster-domain>` |

### Components in `maas-rag` namespace

| Component | Role |
|---|---|
| `milvus` | Standalone vector store |
| `milvus-etcd` | Milvus metadata store |
| `milvus-minio` | Milvus object store |
| `notebook-api` | FastAPI backend — notebook CRUD, document ingest, LlamaStack RAG |
| `notebook-ui` | PatternFly 6 frontend — notebook management, file upload, chat |
| `nomic-embed` | CPU-based embedding InferenceService (nomic-embed-text-v1.5) |

---

## Models

All models are served via KServe `LLMInferenceService` in the `llm` namespace,
behind the MaaS Gateway at:
`http://maas.apps.<cluster-domain>/llm/<model-name>/v1`

| Model | GPU |
|---|---|
| `llama-3-1-8b-instruct-fp8` | 1× L40S |
| `mistral-small-24b-fp8` | 1× L40S |
| `qwen3-4b-instruct` | 1× L40S |
| `phi-4-instruct-w8a8` | 1× L40S |

### API Token

```bash
oc create token default \
  -n maas-default-gateway-tier-enterprise \
  --audience=maas-default-gateway-sa \
  --duration=8760h
```

---

## User Guide

### Credentials

| User | Password | Role |
|---|---|---|
| `admin` | `471917` | cluster-admin, LiteMaaS admin |
| `user1` | `191457` | user |
| `user2` | `191457` | user |

Select the **`htpasswd-maas`** IDP on the OpenShift login screen.
Never use `kube:admin` with LiteMaaS — it has no UID and authentication will fail.

### Accessing Services

| Service | URL |
|---|---|
| OpenShift Console | `https://console-openshift-console.apps.<cluster-domain>` |
| ArgoCD | `https://openshift-gitops-server-openshift-gitops.apps.<cluster-domain>` |
| LiteMaaS | `https://litemaas-litemaas.apps.<cluster-domain>` |
| Grafana | `https://grafana.apps.<cluster-domain>` |
| RAG Notebooks | `https://notebook.apps.<cluster-domain>` |

---

## LiteMaaS Portal

Self-service portal for model subscriptions and API key management.

### Secrets

All secrets generated at deploy-time by `configure.sh` using `openssl rand`.
Nothing is hardcoded or committed to git.

To regenerate on a fresh install:

```bash
oc delete secret litemaas-secrets -n litemaas
./setup/configure.sh
```

### RBAC

| OpenShift Group | LiteMaaS Role |
|---|---|
| `litemaas-admins` | admin |
| `litemaas-readonly` | adminReadonly |
| `litemaas-users` | user |

---

## Configuration Reference

### Feature Flags (`bootstrap/values.yaml`)

| Flag | Default | Effect |
|---|---|---|
| `keycloak.enabled` | `false` | Skips Keycloak — users authenticate via htpasswd only |

