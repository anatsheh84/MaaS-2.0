# MaaS 2.0

GitOps bootstrap for a Model-as-a-Service (MaaS) OpenShift cluster, mirroring the [rhpds/private-llmaas-multitenant](https://github.com/rhpds/private-llmaas-multitenant) reference deployment.

Includes **LiteMaaS** — a self-service portal for LLM subscription and API key management, deployed as a first-class GitOps application.

---

## Architecture

```
bootstrap/          ← Root Helm chart (app-of-apps)
  values.yaml       ← Cluster-specific config (infraID, domain, AMI, etc.)
  templates/
    applications/   ← ArgoCD Application CRs (one per component)
    extra-resources/ ← Shared cluster-scoped resources (groups, gateway, ArgoCD config)

charts/             ← Local Helm charts for each component
  machinesets/      ← AWS MachineSets (workers + GPU)
  cert-manager/     ← cert-manager Operator subscription
  keycloak/         ← RHBK Operator subscription
  keycloak-instance/ ← Keycloak instance (postgres, CR, realm, route, OAuth)
  litemaas/         ← LiteMaaS portal (backend, frontend, LiteLLM, PostgreSQL, Redis)

setup/              ← One-time bootstrap scripts (run once, not GitOps)
  configure.sh      ← Auto-configure cluster values + generate LiteMaaS secrets
  gitops-subscription.yaml  ← Install OpenShift GitOps operator
  cluster-admin-binding.yaml ← Grant cluster-admin to ArgoCD
  bootstrap.yaml    ← Deploy the root bootstrap Application
```

---

## Cluster Details

| Parameter | Value |
|-----------|-------|
| Region | `us-east-2` |
| AZ | `us-east-2b` |
| Platform | AWS |
| OpenShift Version | 4.20 |

---

## Node Topology

| MachineSet | Instance Type | vCPU | RAM | Replicas | Role | Notes |
|---|---|---|---|---|---|---|
| `<infraID>-worker-us-east-2b` | `m6a.4xlarge` | 16 | 64 GB | 5 | Standard workers | General workloads, ArgoCD, operators |
| `<infraID>-worker-gpu-g6e-12xlarge-us-east-2b` | `g6e.12xlarge` | 48 | 192 GB | 1 | **GPU — active** | **4× NVIDIA L40S GPUs, 46 GB VRAM each** |
| `<infraID>-worker-gpu-g6e-2xlarge-us-east-2b` | `g6e.2xlarge` | 8 | 32 GB | 0 | GPU — standby | Scale up for lighter GPU workloads |

### GPU Node Details

The active GPU node (`g6e.12xlarge`) provides:

| Resource | Value |
|---|---|
| GPU model | NVIDIA L40S |
| GPUs per node | **4** |
| VRAM per GPU | 46 GB (46,068 MiB) |
| Total GPU VRAM | **184 GB** |
| Taint | `nvidia.com/gpu=l40-gpu:NoSchedule` |

GPU workloads must tolerate `nvidia.com/gpu=l40-gpu:NoSchedule` and request `nvidia.com/gpu` resources. LLMInferenceServices declare this via their `spec.template.tolerations`.

---

## Models Deployed (OpenShift AI)

All models are served via **KServe LLMInferenceService** in the `llm` namespace, exposed through the MaaS gateway at `maas.apps.<cluster-domain>/llm/<model-name>/v1`.

| Model | Status | GPU Memory |
|---|---|---|
| `llama-3-1-8b-instruct-fp8` | ✅ Running | 1× L40S |
| `mistral-small-24b-fp8` | ✅ Running | 1× L40S |
| `qwen3-4b-instruct` | ✅ Running | 1× L40S |
| `phi-4-instruct-w8a8` | ⏸ Stopped | — |

### Accessing Models — API Key (Single Token for All Models)

The MaaS gateway uses **Kuadrant** for authentication. Every model on this cluster shares the same auth mechanism — you need **one token** that works for all models.

The token is a Kubernetes service account token scoped to the `maas-default-gateway-sa` audience, from one of the tier namespaces. The `enterprise` tier has access to all deployed models.

**Generate the token:**

```bash
oc create token default \
  -n maas-default-gateway-tier-enterprise \
  --audience=maas-default-gateway-sa \
  --duration=8760h    # 1 year
```

**Why one token works for all models:**

Every `LLMInferenceService` deployed by RHOAI automatically creates a `RoleBinding` that grants `post` access to all three tier groups:

```
system:serviceaccounts:maas-default-gateway-tier-free
system:serviceaccounts:maas-default-gateway-tier-premium
system:serviceaccounts:maas-default-gateway-tier-enterprise  ← use this
```

The Kuadrant `AuthPolicy` on the gateway then does two checks:
1. **KubernetesTokenReview** — is this token valid for `audience=maas-default-gateway-sa`?
2. **SubjectAccessReview** — can this service account `post` to `llminferenceservices/<model-name>` in the `llm` namespace?

Since both checks pass for any model in the `llm` namespace, a single enterprise tier token grants access to all of them — current and future.

**Using the token in LiteMaaS (add model form):**

| Field | Value |
|---|---|
| Provider | `openai` |
| Model Name | `<model-name>` (e.g. `qwen3-4b-instruct`) |
| API Base | `http://maas.apps.<cluster-domain>/llm/<model-name>/v1` |
| API Key | *(token from above command)* |

---

## LiteMaaS

[LiteMaaS](https://github.com/anatsheh84/lite-maas) is a self-service portal that lets users subscribe to models, manage API keys, and track usage. It is deployed as a GitOps-managed application (sync wave 8).

### Architecture

```
User → LiteMaaS Frontend (React + PatternFly)
           ↓
       LiteMaaS Backend (Fastify + PostgreSQL)
           ↓
       LiteLLM Gateway (model proxy + budget enforcement)
           ↓
       MaaS Gateway (Kuadrant) → KServe LLMInferenceService
```

### Deployment Namespaces

| Namespace | Purpose |
|---|---|
| `litemaas-test` | **Validation** — active namespace, promoted via GitOps |
| `litemaas` | **Production** — change `litemaas.namespace` in `bootstrap/values.yaml` when ready |

### Components Deployed by Helm Chart

| Component | Image | Description |
|---|---|---|
| Backend | `quay.io/rh-aiservices-bu/litemaas-backend:0.4.0` | Fastify API, PostgreSQL, OAuth |
| Frontend | `quay.io/rh-aiservices-bu/litemaas-frontend:0.4.0` | React + PatternFly 6 UI |
| LiteLLM | `quay.io/rh-aiservices-bu/litellm-non-root:main-v1.81.0-stable-custom` | AI model proxy |
| PostgreSQL | `postgres:16-alpine` | Persistent storage (2 databases: litemaas_db, litellm_db) |
| Redis | `redis:7-alpine` | LiteLLM model/key cache |

### Secrets — Generated by `configure.sh`, Never in Git

All LiteMaaS secrets are generated at deploy-time by `configure.sh` using `openssl rand`. Nothing is hardcoded or committed to the repository.

**What `configure.sh` generates (Step 5c):**

| Secret Key | Description | Generation |
|---|---|---|
| `pg-admin-password` | PostgreSQL admin password | `openssl rand -base64 32` |
| `jwt-secret` | JWT signing key (64 chars) | `openssl rand -base64 64` |
| `oauth-client-secret` | OpenShift OAuthClient secret | `openssl rand -hex 20` |
| `admin-api-key` | LiteMaaS backend management API key | `openssl rand -hex 16` |
| `litellm-api-key` | LiteLLM master key (`sk-` prefixed) | `sk-$(openssl rand -hex 24)` |
| `litellm-master-key` | Encryption key for stored model API keys | `openssl rand -base64 32` |
| `litellm-ui-username` | LiteLLM admin UI username | `admin` (static) |
| `litellm-ui-password` | LiteLLM admin UI password | `openssl rand -base64 16` |

All secrets are stored in a single Kubernetes Secret named `litemaas-secrets` in the `litemaas-test` (or `litemaas`) namespace. The Helm chart references them via `existingSecret: litemaas-secrets` — no secret values appear in the chart templates.

**Manually regenerating secrets** (e.g. for a fresh install):

```bash
# Delete existing secret first
oc delete secret litemaas-secrets -n litemaas-test

# Re-run configure.sh — Step 5c will regenerate everything
./setup/configure.sh <API_URL> <PASSWORD> <AWS_KEY> <AWS_SECRET> <HOSTED_ZONE>
```

**The OAuthClient** (`litemaas-oauth-client`) is also created by `configure.sh` and is NOT managed by ArgoCD — OpenShift identity/auth cluster-scoped resources cause structured merge diff errors in ArgoCD Helm charts.

### RBAC — OpenShift Groups

LiteMaaS maps OpenShift groups to application roles. Groups are managed in `bootstrap/templates/extra-resources/groups.yaml`.

| OpenShift Group | LiteMaaS Role | Capabilities |
|---|---|---|
| `litemaas-admins` | `admin` | Full access: models, users, subscriptions, API keys |
| `litemaas-readonly` | `adminReadonly` | Read-only admin view |
| `litemaas-users` | `user` | Default role for all authenticated users |

The `admin` OpenShift user is automatically added to `litemaas-admins` via `bootstrap/values.yaml`:

```yaml
litemaas:
  adminUsers:
    - admin
```

### Login

LiteMaaS uses OpenShift OAuth. Always log in with a **real htpasswd user** (`admin`, `user1`, `user2`) — never as `kube:admin`.

> **`kube:admin` is a synthetic virtual user** — it has no `metadata.uid` in OpenShift's identity store and cannot be stored in the LiteMaaS database. Attempting to log in as `kube:admin` results in `Authentication failed`.

If your browser has an active `kube:admin` session, use an **incognito window** and select the `htpasswd-maas` IDP at the OpenShift login screen.

### Promoting from `litemaas-test` to `litemaas`

Once validation is complete, change one line in `bootstrap/values.yaml`:

```yaml
litemaas:
  namespace: litemaas-test   # ← change to: litemaas
```

Commit, push, and ArgoCD will redeploy to the production namespace automatically.

---

## Feature Flags

`bootstrap/values.yaml` exposes flags to enable or disable optional components:

| Flag | Default | Effect when `false` |
|------|---------|---------------------|
| `keycloak.enabled` | `false` | Skips `keycloak` (operator) and `keycloak-instance`. Users log in via htpasswd. |

---

## ArgoCD

**UI:** `https://openshift-gitops-server-openshift-gitops.apps.<cluster-domain>`

**Login:** Use the `admin` OpenShift user (htpasswd-maas IDP).

## ArgoCD Application Sync Waves

| Wave | Application | Description |
|------|-------------|-------------|
| 0 | `machinesets` | AWS MachineSets (workers + GPU) |
| 1 | `cert-manager` | cert-manager Operator |
| 2 | `keycloak` | RHBK Operator subscription |
| 2 | `cluster-certificates` | Let's Encrypt wildcard cert + IngressController |
| 3 | `keycloak-instance` | Keycloak DB + CR + realm + route + OCP OAuth |
| 4 | `nvidia-gpu-enablement` | NFD + NVIDIA GPU Operator |
| 4 | `openshift-ai-operator` | RHOAI Operator |
| 4 | `rhcl-operator` | Red Hat Connectivity Link (Kuadrant) |
| 4 | `grafana` | Grafana Operator + dashboard |
| 4 | `devspaces` | OpenShift DevSpaces |
| 4 | `cluster-monitoring` | User workload monitoring config |
| 5 | `openshift-ai` | DataScienceCluster operand |
| 6 | `models` | LLMInferenceServices (llama, mistral, qwen3, phi-4) |
| 6 | `models-as-a-service` | MaaS API + Kuadrant Gateway |
| 7 | `llama-stack-instance` | Per-user Llama Stack playground |
| 7 | `workspace` | Per-user DevSpaces workspace |
| 8 | `kubernetes-mcp-server` | Kubernetes MCP server |
| 8 | `slack-mcp` | Slack MCP server |
| 8 | `litemaas` | **LiteMaaS portal** (backend, frontend, LiteLLM, PostgreSQL, Redis) |

---

## Keycloak

| Item | Value |
|------|-------|
| URL | `https://sso.apps.<cluster-domain>` |
| Realm | `sso` |
| OIDC client | `idp-4-ocp` |
| OCP OAuth provider | `rhbk` |

**Users:**

| Username | Password | Role |
|----------|----------|------|
| `user1` | `Symc@4now` | user |
| `user2` | `Symc@4now` | user |
| `admin` | `Symc@4now` | admin + cluster-admin |

---

## User Onboarding Notes

### Logging in
1. Go to the OpenShift console and click **`htpasswd-maas`** on the login screen
2. Log in with your credentials (`admin`, `user1`, or `user2`)

### First-time RHOAI setup per user
After logging into OpenShift AI, switch to your personal namespace before creating a playground:

- `user1` → select project **`wksp-user1`**
- `user2` → select project **`wksp-user2`**

Then navigate to GenAI Studio and create the playground. Creating it in any other namespace will fail with a permission error.

---

## Deploying to a New Cluster

### Prerequisites

- `oc` CLI installed and in PATH
- `htpasswd` installed (`httpd-tools` on RHEL/Fedora, `apache2-utils` on Debian/Ubuntu)
- Run from the root of the cloned repo
- RHPDS sandbox credentials ready

### One command does everything

```bash
./setup/configure.sh \
  https://api.<new-cluster>:6443 \
  <KUBEADMIN_PASSWORD> \
  <AWS_ACCESS_KEY_ID> \
  <AWS_SECRET_ACCESS_KEY> \
  <HOSTED_ZONE_ID>
```

**That's it. No git commit needed.**

The script handles the full deployment end-to-end:

| Step | What happens |
|------|-------------|
| 1 | Logs into the cluster |
| 2 | Reads all cluster values via `oc` (infraID, AMI, domain, uuid, guid, region, az) |
| 3 | Writes real values to `bootstrap/values.local.yaml` — gitignored, never committed |
| 4 | Creates `cert-manager-aws-creds` secret on the cluster |
| 5 | Installs OpenShift GitOps operator, grants ArgoCD cluster-admin, deploys bootstrap |
| 5b | Creates HTPasswd IDP with `user1`, `user2`, `admin` (password: `MTkxNDU3` / `NDcxOTE3`) |
| **5c** | **Generates LiteMaaS secrets with `openssl rand` and creates `litemaas-secrets`** |
| **5c** | **Creates `litemaas-oauth-client` OAuthClient** |
| 6 | Deploys bootstrap ArgoCD Application |
| 7 | Patches the ArgoCD `helm.valuesObject` with real values — no git commit needed |

> **Why no git commit?**
> `bootstrap/values.yaml` in git is a pure template with empty placeholders.
> Real cluster values (including generated secrets) are injected directly into the
> live ArgoCD Application via `helm.valuesObject` and stored as Kubernetes Secrets —
> they never appear in the repository.

### After the script completes

Monitor ArgoCD progress:

```bash
oc get applications -n openshift-gitops -w
```

One manual step remains — the Slack bot token cannot be automated:

```bash
oc create secret generic slack-mcp-token -n lls-demo \
  --from-literal=slack-bot-token=<SLACK_BOT_TOKEN>
```

---

## Known Operational Notes (Lessons Learned)

### 1. ArgoCD UI Route — Must be explicit in ArgoCD CR spec

The OpenShift GitOps operator does **not** create the ArgoCD server Route by default.
`spec.server.route.enabled: true` must be set explicitly in the ArgoCD CR, or the UI
will be completely unreachable.

`setup/bootstrap.yaml` has `ignoreDifferences` on the ArgoCD CR's entire `/spec` to
prevent ArgoCD from fighting the operator over its auto-managed defaults. To apply
a spec change after initial deploy:

```bash
oc patch argocd openshift-gitops -n openshift-gitops --type merge -p '{"spec": {...}}'
```

### 2. MachineSets — AZ is auto-detected, not hardcoded

`configure.sh` reads the AZ from the cluster's existing MachineSet at deploy time and
injects it into ArgoCD via `helm.valuesObject`. No manual AZ configuration is needed.

### 3. Keycloak — TLS handled at the Route, not the pod

TLS is terminated at the OpenShift router (edge termination) using the cluster's wildcard
Let's Encrypt cert — not at the Keycloak pod. The Keycloak CR uses `httpEnabled: true`
so the pod serves plain HTTP internally.

| Resource | Owner |
|---|---|
| `keycloak-service` | RHBK operator — ArgoCD does NOT touch this |
| `Keycloak` CR | ArgoCD |
| Route | ArgoCD |
| PostgreSQL | ArgoCD |

### 4. PostgreSQL PVC — WaitForFirstConsumer Deadlock

With `gp3-csi` (`volumeBindingMode: WaitForFirstConsumer`), the PVC stays `Pending`
until a pod mounts it. PVC and Deployment are placed in the **same sync wave** so
ArgoCD applies them together and the PVC binds immediately.

### 5. cert-manager Ingress Cert — Double `apps.` Bug

`cluster.domain` is already `apps.cluster-xxx...` — do not prefix it with `apps.`
again in the Certificate template or the CN becomes `apps.apps.cluster-xxx...`,
which breaks TLS on every route cluster-wide.

### 6. models-as-a-service Gateway

The `maas-default-gateway` uses `gatewayClassName: openshift-default`. This GatewayClass
is **not** provisioned automatically — it is explicitly created in
`bootstrap/templates/extra-resources/gatewayclass.yaml`.

### 7. LiteMaaS — ArgoCD cannot manage OpenShift identity/auth resources in Helm charts

`user.openshift.io/v1 Kind=Group` and `oauth.openshift.io/v1 Kind=OAuthClient` cause
`unable to resolve parseableType` ComparisonErrors when included in a Helm chart managed
by ArgoCD. These are cluster-scoped OpenShift identity resources that ArgoCD's structured
merge diff engine cannot handle.

**Fix applied:**
- OpenShift Groups (`litemaas-admins`, `litemaas-readonly`, `litemaas-users`) are managed
  in `bootstrap/templates/extra-resources/groups.yaml` (part of the bootstrap chart which
  handles them correctly).
- `OAuthClient` is created by `configure.sh` and is not tracked by ArgoCD at all.
- The `litemaas` Helm chart contains only namespace-scoped resources (Deployments,
  Services, Routes, PVCs, RoleBindings, ConfigMaps) which ArgoCD handles cleanly.

### 8. LiteMaaS — Do not log in as `kube:admin`

`kube:admin` is a synthetic virtual user — OpenShift's `/apis/user.openshift.io/v1/users/~`
returns no `metadata.uid` for it. LiteMaaS requires a non-null `oauth_id` when creating
a user record, causing a database constraint violation and an `Authentication failed` error.

Always use `admin` (or `user1`/`user2`) from the `htpasswd-maas` IDP.
