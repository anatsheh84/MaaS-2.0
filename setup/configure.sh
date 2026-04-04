#!/usr/bin/env bash
# =============================================================================
# configure.sh — Configure and deploy MaaS 2.0 on a new OpenShift cluster
#
# WHAT IT DOES:
#   1. Logs into the cluster
#   2. Reads all cluster-specific values via oc (infraID, AMI, domain, etc.)
#   3. Writes real values to bootstrap/values.local.yaml  ← NEVER committed
#   4. Creates the cert-manager-aws-creds secret on the cluster
#   5. Installs OpenShift GitOps operator and waits for it to be ready
#   6. Grants ArgoCD cluster-admin
#   7. Applies setup/bootstrap.yaml to create the ArgoCD bootstrap Application
#   8. Patches the bootstrap Application's helm.valuesObject with real values
#      so ArgoCD renders all templates correctly — without any git commit
#
# USAGE:
#   ./setup/configure.sh \
#     <API_URL> \
#     <KUBEADMIN_PASSWORD> \
#     <AWS_ACCESS_KEY_ID> \
#     <AWS_SECRET_ACCESS_KEY> \
#     <HOSTED_ZONE_ID>
#
# EXAMPLE:
#   ./setup/configure.sh \
#     https://api.cluster-abc12.abc12.sandbox123.opentlc.com:6443 \
#     myPassword123 \
#     AKIAIOSFODNN7EXAMPLE \
#     wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
#     Z1234567890ABC
#
# PREREQUISITES:
#   - oc CLI installed and in PATH
#   - Run from the root of the MaaS-2.0 repo
# =============================================================================
set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${BOLD}━━━ $* ${NC}"; }


# ── Argument validation ────────────────────────────────────────────────────────
if [[ $# -ne 5 ]]; then
  echo ""
  echo -e "${BOLD}Usage:${NC}"
  echo "  ./setup/configure.sh <API_URL> <KUBEADMIN_PASSWORD> <AWS_ACCESS_KEY_ID> <AWS_SECRET_ACCESS_KEY> <HOSTED_ZONE_ID>"
  echo ""
  echo -e "${YELLOW}Example:${NC}"
  echo "  ./setup/configure.sh \\"
  echo "    https://api.cluster-abc12.abc12.sandbox123.opentlc.com:6443 \\"
  echo "    myPassword123 \\"
  echo "    AKIAIOSFODNN7EXAMPLE \\"
  echo "    wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \\"
  echo "    Z1234567890ABC"
  echo ""
  echo -e "${YELLOW}Where to find the values:${NC}"
  echo "  API_URL            → RHPDS sandbox info page"
  echo "  KUBEADMIN_PASSWORD → RHPDS sandbox info page"
  echo "  AWS_ACCESS_KEY_ID  → RHPDS sandbox AWS credentials"
  echo "  AWS_SECRET_ACCESS_KEY → RHPDS sandbox AWS credentials"
  echo "  HOSTED_ZONE_ID     → RHPDS sandbox AWS credentials"
  echo ""
  exit 1
fi

API_URL="$1"
OC_PASSWORD="$2"
AWS_ACCESS_KEY_ID="$3"
AWS_SECRET_ACCESS_KEY="$4"
HOSTED_ZONE_ID="$5"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_VALUES="$REPO_ROOT/bootstrap/values.local.yaml"

command -v oc &>/dev/null || error "'oc' not found. Install the OpenShift CLI first."

echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}${BOLD}║   MaaS 2.0 — Cluster Configuration Script   ║${NC}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════╝${NC}"


# ── Step 1: Login ──────────────────────────────────────────────────────────────
step "Step 1/6 — Login to cluster"
info "Logging in to: $API_URL"
oc login "$API_URL" -u kubeadmin -p "$OC_PASSWORD" --insecure-skip-tls-verify=true \
  &>/dev/null || error "Login failed. Check API_URL and password."
success "Logged in successfully"

# ── Step 2: Read cluster values ────────────────────────────────────────────────
step "Step 2/6 — Reading cluster values"

INFRA_ID=$(oc get infrastructure cluster \
  -o jsonpath='{.status.infrastructureName}')
API_DOMAIN=$(echo "$API_URL" | sed 's|https://||' | sed 's|:6443||')
APPS_DOMAIN=$(oc get ingresses.config.openshift.io cluster \
  -o jsonpath='{.spec.domain}')
REGION=$(oc get infrastructure cluster \
  -o jsonpath='{.status.platformStatus.aws.region}')
AMI=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.ami.id}')
UUID=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.tags[?(@.name=="uuid")].value}')
GUID=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.tags[?(@.name=="guid")].value}')
AZ=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.placement.availabilityZone}')

echo ""
echo -e "  infraID    : ${GREEN}$INFRA_ID${NC}"
echo -e "  apiDomain  : ${GREEN}$API_DOMAIN${NC}"
echo -e "  appsDomain : ${GREEN}$APPS_DOMAIN${NC}"
echo -e "  region     : ${GREEN}$REGION${NC}"
echo -e "  az         : ${GREEN}$AZ${NC}"
echo -e "  ami        : ${GREEN}$AMI${NC}"
echo -e "  guid       : ${GREEN}$GUID${NC}"
echo -e "  uuid       : ${GREEN}$UUID${NC}"
echo ""
success "All cluster values collected"


# ── Step 3: Write values.local.yaml (never committed) ─────────────────────────
step "Step 3/6 — Writing bootstrap/values.local.yaml"

cat > "$LOCAL_VALUES" <<EOF
# !! THIS FILE IS GITIGNORED — NEVER COMMIT !!
# Generated by configure.sh on $(date -u '+%Y-%m-%d %H:%M UTC')
# Cluster: $INFRA_ID

cluster:
  infraID: "$INFRA_ID"
  apiUrl: "$API_URL"
  apiDomain: "$API_DOMAIN"
  domain: "$APPS_DOMAIN"
  region: "$REGION"
  platform: AWS

aws:
  ami: "$AMI"
  guid: "$GUID"
  uuid: "$UUID"
  az: "$AZ"

deployer:
  domain: "$APPS_DOMAIN"

certManager:
  issuerName: letsencrypt-production-ec2
  email: rhpds-admins@redhat.com
  route53:
    accessKeyID: "$AWS_ACCESS_KEY_ID"
    hostedZoneID: "$HOSTED_ZONE_ID"
    region: "$REGION"
    credentialsSecretName: cert-manager-aws-creds
  dnsZones:
    - "$APPS_DOMAIN"
    - "$API_DOMAIN"
EOF

success "bootstrap/values.local.yaml written (gitignored — safe)"

# ── Step 4: Create cert-manager-aws-creds secret ──────────────────────────────
step "Step 4/6 — Creating cert-manager-aws-creds secret"
oc get namespace cert-manager &>/dev/null || oc create namespace cert-manager
if oc get secret cert-manager-aws-creds -n cert-manager &>/dev/null; then
  warn "cert-manager-aws-creds already exists — skipping"
else
  oc create secret generic cert-manager-aws-creds -n cert-manager \
    --from-literal=aws_secret_access_key="$AWS_SECRET_ACCESS_KEY"
  success "cert-manager-aws-creds secret created"
fi


# ── Step 5: Install GitOps + bootstrap Application ────────────────────────────
step "Step 5/6 — Installing OpenShift GitOps and deploying bootstrap"

info "Installing OpenShift GitOps operator..."
oc apply -f "$REPO_ROOT/setup/gitops-subscription.yaml"

info "Waiting for GitOps operator to be ready (~2 min)..."
until oc get csv -n openshift-gitops 2>/dev/null | grep -q "Succeeded"; do
  echo -n "."; sleep 10
done
echo ""
success "GitOps operator is ready"

info "Granting cluster-admin to ArgoCD..."
oc apply -f "$REPO_ROOT/setup/cluster-admin-binding.yaml"
success "cluster-admin granted"

info "Deploying bootstrap Application..."
oc apply -f "$REPO_ROOT/setup/bootstrap.yaml"
success "Bootstrap Application deployed"

# Wait for ArgoCD to be ready before patching
info "Waiting for ArgoCD server to be ready..."
oc rollout status deployment/openshift-gitops-server \
  -n openshift-gitops --timeout=180s &>/dev/null || true
sleep 5

# ── Step 6: Patch bootstrap Application with real values ──────────────────────
step "Step 6/6 — Injecting real values into ArgoCD bootstrap Application"
info "Patching helm.valuesObject — values go directly to ArgoCD, not to git"

# Build the JSON patch with all cluster-specific values
VALUES_PATCH=$(cat <<EOF
{
  "spec": {
    "source": {
      "helm": {
        "valuesObject": {
          "cluster": {
            "infraID": "$INFRA_ID",
            "apiUrl": "$API_URL",
            "apiDomain": "$API_DOMAIN",
            "domain": "$APPS_DOMAIN",
            "region": "$REGION",
            "platform": "AWS"
          },
          "aws": {
            "ami": "$AMI",
            "guid": "$GUID",
            "uuid": "$UUID",
            "az": "$AZ"
          },
          "deployer": {
            "domain": "$APPS_DOMAIN"
          },
          "certManager": {
            "issuerName": "letsencrypt-production-ec2",
            "email": "rhpds-admins@redhat.com",
            "route53": {
              "accessKeyID": "$AWS_ACCESS_KEY_ID",
              "hostedZoneID": "$HOSTED_ZONE_ID",
              "region": "$REGION",
              "credentialsSecretName": "cert-manager-aws-creds"
            },
            "dnsZones": ["$APPS_DOMAIN", "$API_DOMAIN"]
          }
        }
      }
    }
  }
}
EOF
)

oc patch application bootstrap -n openshift-gitops \
  --type merge -p "$VALUES_PATCH"

success "ArgoCD bootstrap Application patched with real cluster values"


# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   Configuration complete!                    ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}What was done:${NC}"
echo "  ✔  Logged into cluster ($INFRA_ID)"
echo "  ✔  Collected all cluster values via oc"
echo "  ✔  Written to bootstrap/values.local.yaml  (gitignored — never committed)"
echo "  ✔  Created cert-manager-aws-creds secret on cluster"
echo "  ✔  Installed OpenShift GitOps operator"
echo "  ✔  Deployed bootstrap ArgoCD Application"
echo "  ✔  Patched bootstrap with real values (no git commit needed)"
echo ""
echo -e "${YELLOW}What to do next:${NC}"
echo ""
echo "  1. Monitor ArgoCD progress:"
echo "       oc get applications -n openshift-gitops -w"
echo ""
echo "  2. ArgoCD UI:"
echo "       https://openshift-gitops-server-openshift-gitops.$APPS_DOMAIN"
echo ""
echo "  3. When the slack-mcp Application is deployed, create the Slack token secret:"
echo "       oc create secret generic slack-mcp-token -n lls-demo \\"
echo "         --from-literal=slack-bot-token=<YOUR_SLACK_BOT_TOKEN>"
echo ""
echo -e "  ${CYAN}Note: No git commit was needed. Real values live only on the cluster.${NC}"
echo "  The values.local.yaml file is for your local reference only."
echo ""
