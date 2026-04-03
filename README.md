# MaaS 2.0

GitOps bootstrap for a Model-as-a-Service (MaaS) OpenShift cluster, mirroring the [rhpds/private-llmaas-multitenant](https://github.com/rhpds/private-llmaas-multitenant) reference deployment.

## Architecture

```
bootstrap/          ← Root Helm chart (app-of-apps)
  values.yaml       ← Cluster-specific config (infraID, domain, AMI, etc.)
  templates/
    applications/   ← ArgoCD Application CRs (one per component)
    extra-resources/ ← ArgoCD CR minimal config

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
| InfraID | `cluster-mfm68-hsq8s` |
| Domain | `apps.cluster-mfm68.mfm68.sandbox2356.opentlc.com` |
| Region | `us-east-2` |
| AZ | `us-east-2c` (single-AZ — only zone with VPC subnets) |
| AMI | `ami-021a620474c1cd2fe` |

## Node Topology

| MachineSet | Instance Type | Replicas | AZ | Notes |
|------------|--------------|----------|----|-------|
| `cluster-mfm68-hsq8s-worker-us-east-2c` | m6a.4xlarge | 5 | us-east-2c | Standard workers |
| `cluster-mfm68-hsq8s-worker-gpu-us-east-2c` | g6e.2xlarge | 1 | us-east-2c | NVIDIA L40S; taint `nvidia.com/gpu=l40-gpu:NoSchedule` |

> **Note:** MaaS reference cluster uses 3 AZs (2a×1, 2b×2, 2c×2). This cluster was
> installed single-AZ so all nodes land in us-east-2c. If multi-AZ subnets are added
> later, update `charts/machinesets/values.yaml` to split workers across AZs.

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
| URL | `https://sso.apps.cluster-mfm68.mfm68.sandbox2356.opentlc.com` |
| Realm | `sso` |
| OIDC client | `idp-4-ocp` |
| OCP OAuth provider | `rhbk` |

**Users** (same as MaaS reference):

| Username | Password | Role |
|----------|----------|------|
| `user1` | `MTkxNDU3` | user |
| `user2` | `MTkxNDU3` | user |
| `admin` | `NDcxOTE3` | admin |

## Known Operational Notes (Lessons Learned)

### 1. MachineSets — Single-AZ only
The VPC was provisioned with private subnets only in `us-east-2c`. Attempting to create
MachineSets in `us-east-2a`/`us-east-2b` fails with *"no subnet IDs were found"*.
All 5 workers + GPU node are in `us-east-2c`.

### 2. Keycloak — Service CA TLS
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

### 3. Keycloak Route — Service Name
The route must point to `keycloak-service` (not `keycloak`). The `keycloak` service
does not exist in this operator version. The `https` port on `keycloak-service` (8443)
is the correct backend.

### 4. PostgreSQL PVC — WaitForFirstConsumer Deadlock
With `gp3-csi` (`volumeBindingMode: WaitForFirstConsumer`), the PVC stays `Pending`
until a pod mounts it. ArgoCD waits for PVC health before proceeding to the next
sync wave, causing a deadlock if PVC and Deployment are in different waves.

**Fix:** PVC and Deployment are in **the same sync wave (-1)** in `postgres.yaml`.
ArgoCD applies them together; the pod mounts the PVC; the PVC binds.

## Redeployment Procedure

On a fresh cluster (same GUID/infraID) the only manual steps are:

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

# Everything else is GitOps-driven from this point.
# Monitor progress:
oc get applications -n openshift-gitops -w
```

ArgoCD will automatically:
- Create MachineSets → nodes provision (~10 min)
- Install cert-manager and Keycloak operators
- Deploy Keycloak (postgres → Keycloak CR → realm import → route → OCP OAuth)
