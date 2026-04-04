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
| `cluster-mfm68-hsq8s-worker-us-east-2c` | m6a.4xlarge | 5 | us-east-2c | Standard workers |
| `cluster-mfm68-hsq8s-worker-gpu-us-east-2c` | g6e.2xlarge | 1 | us-east-2c | NVIDIA L40S; taint `nvidia.com/gpu=l40-gpu:NoSchedule` |

> **Note:** MaaS reference cluster uses 3 AZs (2a×1, 2b×2, 2c×2). This cluster was
> installed single-AZ so all nodes land in us-east-2c. If multi-AZ subnets are added
> later, update `charts/machinesets/values.yaml` to split workers across AZs.

## ArgoCD

**UI:** `https://openshift-gitops-server-openshift-gitops.apps.<cluster-domain>`

**Login:** Use the `kubeadmin` password or the `admin` OpenShift user.

## ArgoCD Application Sync Waves

| Wave | Application | Description |
|------|-------------|-------------|
| 0 | `machinesets` | AWS MachineSets (no wave = wave 0) |
| 1 | `cert-manager` | cert-manager Operator |
| 2 | `keycloak` | RHBK Operator subscription |
| 3 | `keycloak-instance` | Keycloak DB + CR + realm + route + OCP OAuth |

## Keycloak

| Item | Value |
|------|-------|
| URL | `https://sso.apps.<cluster-domain>` |
| Realm | `sso` |
| OIDC client | `idp-4-ocp` |
| OCP OAuth provider | `rhbk` |

**Users** (same as MaaS reference):

| Username | Role |
|----------|------|
| `user1` | user |
| `user2` | user |
| `admin` | admin |

## Known Operational Notes (Lessons Learned)

### 1. ArgoCD UI Route — Must be explicit in ArgoCD CR spec

The OpenShift GitOps operator does **not** create the ArgoCD server Route by default.
`spec.server.route.enabled: true` must be set explicitly in the ArgoCD CR, or the UI
will be completely unreachable.

**Additional gotcha:** `setup/bootstrap.yaml` has `ignoreDifferences` on the ArgoCD CR's
entire `/spec` to prevent ArgoCD from fighting the operator over its auto-managed defaults
(grafana, prometheus, redis, etc.). This means ArgoCD **will not** re-apply spec changes
automatically — it ignores all `/spec` drift. To apply a spec change after initial deploy:

```bash
oc patch argocd openshift-gitops -n openshift-gitops --type merge -p '{"spec": {...}}'
```

The route spec is set in `bootstrap/templates/extra-resources/gitops.yaml` and will be
applied correctly on a **fresh** deploy. On an existing cluster, patch manually as above.

### 2. MachineSets — Single-AZ only
The VPC was provisioned with private subnets only in `us-east-2c`. Attempting to create
MachineSets in `us-east-2a`/`us-east-2b` fails with *"no subnet IDs were found"*.
All 5 workers + GPU node are in `us-east-2c`.

### 3. Keycloak — Service CA TLS
The RHBK operator (v26.2) creates a service named `<cr-name>-service` (`keycloak-service`)
but does **not** add the OpenShift Service CA annotation automatically. Without this,
the `keycloak-tls` Secret is never generated and the pod fails to start.

**Fix (codified in `keycloak-service-patch.yaml`):** ArgoCD applies a metadata-only SSA
patch to `keycloak-service` that adds:
```
service.beta.openshift.io/serving-cert-secret-name: keycloak-tls
```
Service CA then auto-generates `keycloak-tls`. Only the annotation is SSA-owned by
ArgoCD; the operator owns all `spec` fields — no conflict.

### 4. Keycloak Route — Service Name
The route must point to `keycloak-service` (not `keycloak`). The `keycloak` service
does not exist in this operator version. The `https` port on `keycloak-service` (8443)
is the correct backend.

### 5. PostgreSQL PVC — WaitForFirstConsumer Deadlock
With `gp3-csi` (`volumeBindingMode: WaitForFirstConsumer`), the PVC stays `Pending`
until a pod mounts it. ArgoCD waits for PVC health before proceeding to the next
sync wave, causing a deadlock if PVC and Deployment are in different waves.

**Fix:** PVC and Deployment are in **the same sync wave (-1)** in `postgres.yaml`.
ArgoCD applies them together; the pod mounts the PVC; the PVC binds.

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

## Manual Secrets Required Before Redeployment

Two secrets contain sensitive credentials that must NOT be committed to git.
Create them on the cluster **before** applying `setup/bootstrap.yaml`:

```bash
# 1. cert-manager Route53 credentials (for Let's Encrypt DNS-01 challenge)
#    Get the secret access key from your RHPDS sandbox credentials page
oc create secret generic cert-manager-aws-creds -n cert-manager \
  --from-literal=aws_secret_access_key=<AWS_SECRET_ACCESS_KEY>

# 2. Slack MCP bot token (for the slack-mcp Application)
oc create secret generic slack-mcp-token -n lls-demo \
  --from-literal=slack-bot-token=<SLACK_BOT_TOKEN>
```

> **Note:** On RHPDS sandboxes, `cert-manager-aws-creds` may already be provisioned
> by the platform. Check `oc get secret cert-manager-aws-creds -n cert-manager` first.

## Redeployment Procedure

### Step 1 — Update `bootstrap/values.yaml`

This is the **only file** you need to change between clusters. All charts and
Applications derive their configuration from it — nothing else is hardcoded.

**Must update on every new cluster:**

| Value | How to get it |
|-------|--------------|
| `cluster.infraID` | `oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}'` |
| `cluster.apiUrl` | New cluster API URL |
| `cluster.apiDomain` | `api.<new-cluster-domain>` |
| `cluster.domain` | `apps.<new-cluster-domain>` |
| `deployer.domain` | Same as `cluster.domain` |
| `aws.ami` | `oc get machineset -n openshift-machine-api -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.ami.id}'` |
| `aws.guid` | RHPDS sandbox GUID |
| `aws.uuid` | `oc get machineset -n openshift-machine-api -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.tags[?(@.name=="uuid")].value}'` |
| `certManager.route53.accessKeyID` | RHPDS sandbox AWS credentials |
| `certManager.route53.hostedZoneID` | RHPDS sandbox AWS credentials |
| `certManager.dnsZones` | `[apps.<new-domain>, api.<new-domain>]` |

**Stays the same on every cluster:**

`cluster.region`, `cluster.platform`, `aws.az`, `llmaas.repoURL`,
`llmaas.targetRevision`, `certManager.issuerName`, `certManager.email`,
`users.count`, `users.prefix`, `bootstrap.repoURL`, `bootstrap.targetRevision`

### Step 2 — Run the deployment

```bash
# 1. Install GitOps operator
oc apply -f setup/gitops-subscription.yaml

# 2. Wait for operator to be ready (~2 min)
oc wait csv -n openshift-gitops \
  --for=jsonpath='{.status.phase}'=Succeeded \
  -l operators.coreos.com/openshift-gitops-operator.openshift-gitops \
  --timeout=300s

# 3. Grant ArgoCD cluster-admin
oc apply -f setup/cluster-admin-binding.yaml

# 4. Deploy the bootstrap Application (app-of-apps)
oc apply -f setup/bootstrap.yaml

# Monitor progress
oc get applications -n openshift-gitops -w
```

ArgoCD will automatically:
- Create MachineSets → nodes provision (~10 min)
- Install cert-manager and Keycloak operators
- Deploy Keycloak (postgres → Keycloak CR → realm import → route → OCP OAuth)
