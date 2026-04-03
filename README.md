# MaaS 2.0 — Models-as-a-Service on OpenShift

GitOps-driven deployment of a Models-as-a-Service platform on OpenShift, bootstrapped via ArgoCD.

## Architecture

This repo follows the **app-of-apps** pattern:

```
bootstrap/              # Root ArgoCD Application (Helm chart)
├── Chart.yaml
├── values.yaml         # Cluster-specific configuration
└── templates/
    ├── extra-resources/ # Cluster-level resources (ArgoCD config)
    └── *.yaml          # ArgoCD Application definitions (one per component)
```

## Phases

- **Phase 1**: GitOps bootstrap (OpenShift GitOps + ArgoCD)
- **Phase 2**: MachineSets (standard workers + GPU nodes)
- **Phase 3**: Gap operators (cert-manager, Keycloak, Service Mesh deps)
- **Phase 4**: MaaS workloads (GPU enablement, RHOAI, models, apps)

## Target Cluster

- **Platform**: AWS (us-east-2)
- **InfraID**: `cluster-mfm68-hsq8s`
- **API**: `https://api.cluster-mfm68.mfm68.sandbox2356.opentlc.com:6443`
