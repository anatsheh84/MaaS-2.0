# MaaS 2.0

GitOps bootstrap for a Model-as-a-Service (MaaS) OpenShift cluster, mirroring the [rhpds/private-llmaas-multitenant](https://github.com/rhpds/private-llmaas-multitenant) reference deployment.

## Architecture

```
bootstrap/          ← Root Helm chart (app-of-apps)
  values.yaml       ← Cluster-specific config (infraID, domain, AMI, etc.)
  templates/
    applications/   ← ArgoCD Application CRs (one per component)
    extra-resources/ ← ArgoCD CR config (server route, resourceTrackingMethod)

charts/             ← Local Helm charts for each component
  machinesets/      ← AWS MachineSets (workers + GPU)
  cert-manager/     ← cert-manager Operator subscription
  keycloak/         ← RHBK Operator subscription
  keycloak-instance/ ← Keycloak instance (postgres, CR, realm, route, OAuth)

setup/              ← One-time manual bootstrap manifests (run once, not GitOps)
  configure.sh      ← Auto-populate values.yaml for a new cluster
  gitops-subscription.yaml  ← Install OpenShift GitOps operator
  cluster-admin-binding.yaml ← Grant cluster-admin to ArgoCD
  bootstrap.yaml    ← Deploy the root bootstrap Application
```

## Cluster Details

| Parameter | Value |
|-----------|-------|
| Region | `us-east-2` |
| AZ | `us-east-2c` (single-AZ — only zone with VPC subnets) |

## Node Topology

| MachineSet | Instance Type | Replicas | AZ | Notes |
|------------|--------------|----------|----|-------|
| `<infraID>-worker-us-east-2c` | m6a.4xlarge | 5 | us-east-2c | Standard workers |
| `<infraID>-worker-gpu-us-east-2c` | g6e.2xlarge | 1 | us-east-2c | NVIDIA L40S; taint `nvidia.com/gpu=l40-gpu:NoSchedule` |

> **Note:** MaaS reference cluster uses 3 AZs (2a×1, 2b×2, 2c×2). This cluster was
> installed single-AZ so all nodes land in us-east-2c. If multi-AZ subnets are added
> later, update `charts/machinesets/values.yaml` to split workers across AZs.

## ArgoCD

**UI:** `https://openshift-gitops-server-openshift-gitops.apps.<cluster-domain>`

**Login:** Use the `kubeadmin` password or the `admin` OpenShift user.

## ArgoCD Application Sync Waves

| Wave | Application | Description |
|------|-------------|-------------|
| 0 | `machinesets` | AWS MachineSets |
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
| 6 | `models` | LLMInferenceService (qwen3-4b-instruct) |
| 6 | `models-as-a-service` | MaaS API + Gateway |
| 7 | `llama-stack-instance` | Per-user Llama Stack playground |
| 7 | `workspace` | Per-user DevSpaces workspace |
| 8 | `kubernetes-mcp-server` | Kubernetes MCP server |
| 8 | `slack-mcp` | Slack MCP server |

## Keycloak

| Item | Value |
|------|-------|
| URL | `https://sso.apps.<cluster-domain>` |
| Realm | `sso` |
| OIDC client | `idp-4-ocp` |
| OCP OAuth provider | `rhbk` |

**Users:**

| Username | Role |
|----------|------|
| `user1` | user |
| `user2` | user |
| `admin` | admin |


## User Onboarding Notes

### Logging in
1. Go to the OpenShift console and click **`rhbk`** on the login screen
2. Log in with your Keycloak credentials (user1, user2, or admin)

### First-time RHOAI setup per user
After logging into OpenShift AI, the dashboard defaults to the last active project.
**Before creating a playground**, switch to your personal namespace:

- `user1` → select project **`wksp-user1`**
- `user2` → select project **`wksp-user2`**

Then navigate to GenAI Studio and create the playground. Creating it in any other
namespace (e.g. `grafana`, `default`) will fail with a permission error because
users only have `edit` access in their own `wksp-<username>` namespace.

---

## Deploying to a New Cluster

### Prerequisites

- `oc` CLI installed and in PATH
- Run from the root of the cloned repo
- RHPDS sandbox credentials ready (API URL, kubeadmin password, AWS access key, AWS secret key, hosted zone ID)

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
| 5 | Installs OpenShift GitOps operator, grants ArgoCD cluster-admin, deploys bootstrap Application |
| 6 | Patches the ArgoCD bootstrap Application's `helm.valuesObject` with real values so ArgoCD renders all templates correctly — **without touching git** |

> **Why no git commit?**
> `bootstrap/values.yaml` in git is a pure template with empty placeholders.
> Real cluster values are injected directly into the live ArgoCD Application via
> `helm.valuesObject` — they never leave your cluster or appear in the repo.

### After the script completes

ArgoCD automatically deploys everything in sync-wave order. Monitor progress:

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
prevent ArgoCD from fighting the operator over its auto-managed defaults. This means
ArgoCD will **not** re-apply spec changes automatically on an existing cluster. To apply
a spec change after initial deploy:

```bash
oc patch argocd openshift-gitops -n openshift-gitops --type merge -p '{"spec": {...}}'
```

### 2. MachineSets — Single-AZ only
The VPC is provisioned with private subnets only in `us-east-2c`. Attempting to create
MachineSets in `us-east-2a`/`us-east-2b` fails with *"no subnet IDs were found"*.
All 5 workers + GPU node are in `us-east-2c`.

### 3. Keycloak — Service CA TLS
The RHBK operator (v26.2) creates a service named `keycloak-service` but does **not**
add the OpenShift Service CA annotation automatically. Without it, `keycloak-tls` is
never generated and the pod fails to mount the volume.

Fix is codified in `keycloak-service-patch.yaml` — ArgoCD applies a metadata-only SSA
patch that adds `service.beta.openshift.io/serving-cert-secret-name: keycloak-tls`.
No manual intervention needed on a fresh deploy.

### 4. Keycloak Route — Service Name
The route must point to `keycloak-service` (not `keycloak`). The `keycloak` service
does not exist in RHBK operator v26.2. The `https` port on `keycloak-service` (8443)
is the correct backend.

### 5. PostgreSQL PVC — WaitForFirstConsumer Deadlock
With `gp3-csi` (`volumeBindingMode: WaitForFirstConsumer`), the PVC stays `Pending`
until a pod mounts it. ArgoCD health-gates between sync waves, so if the PVC and
Deployment are in different waves the deployment never starts.

Fix: PVC and Deployment are in the **same sync wave (-1)** in `postgres.yaml` so
ArgoCD applies them together and the PVC binds immediately.

### 6. cert-manager Ingress Cert — Double `apps.` Bug
`cluster.domain` is already `apps.cluster-xxx...` — do not prefix it with `apps.`
again in the Certificate template or the CN becomes `apps.apps.cluster-xxx...`,
which breaks TLS on every route cluster-wide.

### 7. models-as-a-service Gateway
The `maas-default-gateway` uses `gatewayClassName: openshift-default`. This GatewayClass
is **not** provisioned automatically — it is explicitly created in
`bootstrap/templates/extra-resources/gatewayclass.yaml`.
