# Cluster Setup

These files bootstrap GitOps on a fresh OpenShift cluster.

## Prerequisites

1. Install the OpenShift GitOps operator:

```bash
oc apply -f setup/gitops-subscription.yaml
```

2. Wait for the operator to be ready:

```bash
oc wait csv -n openshift-gitops -l operators.coreos.com/openshift-gitops-operator.openshift-operators --for=jsonpath='{.status.phase}'=Succeeded --timeout=300s
```

3. Grant cluster-admin to ArgoCD:

```bash
oc apply -f setup/cluster-admin-binding.yaml
```

4. Deploy the bootstrap Application:

```bash
oc apply -f setup/bootstrap.yaml
```

From this point, everything else is managed by GitOps via the `bootstrap/` chart.
