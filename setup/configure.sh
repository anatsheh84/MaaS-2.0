#!/usr/bin/env bash
# =============================================================================
# configure.sh — Auto-populate bootstrap/values.yaml for a new cluster
#
# USAGE:
#   ./setup/configure.sh <API_URL> <KUBEADMIN_PASSWORD> <AWS_ACCESS_KEY_ID> <AWS_SECRET_ACCESS_KEY> <HOSTED_ZONE_ID>
#
# EXAMPLE:
#   ./setup/configure.sh \
#     https://api.cluster-abc12.abc12.sandbox123.opentlc.com:6443 \
#     myPassword123 \
#     AKIAIOSFODNN7EXAMPLE \
#     wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY \
#     Z1234567890ABC
#
# WHAT IT DOES:
#   1. Logs into the cluster using the provided credentials
#   2. Reads all cluster-specific values (infraID, AMI, domain, uuid, guid)
#   3. Updates bootstrap/values.yaml in place
#   4. Creates the cert-manager-aws-creds secret on the cluster
#   5. Prints a summary of everything that was changed
#
# PREREQUISITES:
#   - oc CLI installed and in PATH
#   - Run from the root of the MaaS-2.0 repo
#   - yq installed (brew install yq) for YAML editing
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Argument validation ───────────────────────────────────────────────────────
if [[ $# -ne 5 ]]; then
  echo ""
  echo -e "${RED}Usage:${NC}"
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
  exit 1
fi

API_URL="$1"
OC_PASSWORD="$2"
AWS_ACCESS_KEY_ID="$3"
AWS_SECRET_ACCESS_KEY="$4"
HOSTED_ZONE_ID="$5"

VALUES_FILE="$(dirname "$0")/../bootstrap/values.yaml"
[[ -f "$VALUES_FILE" ]] || error "bootstrap/values.yaml not found. Run from the repo root."
command -v oc  &>/dev/null || error "'oc' not found. Install the OpenShift CLI first."
command -v yq  &>/dev/null || error "'yq' not found. Install with: brew install yq"


# ── Step 1: Login ─────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}════════════════════════════════════════════${NC}"
echo -e "${CYAN}  MaaS 2.0 — Cluster Configuration Script  ${NC}"
echo -e "${CYAN}════════════════════════════════════════════${NC}"
echo ""
info "Logging into cluster: $API_URL"
oc login "$API_URL" -u kubeadmin -p "$OC_PASSWORD" --insecure-skip-tls-verify=true &>/dev/null \
  || error "Login failed. Check API_URL and password."
success "Logged in successfully"

# ── Step 2: Read cluster values ───────────────────────────────────────────────
echo ""
info "Reading cluster values from OpenShift..."

INFRA_ID=$(oc get infrastructure cluster -o jsonpath='{.status.infrastructureName}')
API_DOMAIN=$(echo "$API_URL" | sed 's|https://||' | sed 's|:6443||')
APPS_DOMAIN=$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')
REGION=$(oc get infrastructure cluster -o jsonpath='{.status.platformStatus.aws.region}')
AMI=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.ami.id}')
UUID=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.tags[?(@.name=="uuid")].value}')
GUID=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.tags[?(@.name=="guid")].value}')
AZ=$(oc get machineset -n openshift-machine-api \
  -o jsonpath='{.items[0].spec.template.spec.providerSpec.value.placement.availabilityZone}')

echo ""
echo -e "${YELLOW}Values read from cluster:${NC}"
echo "  infraID      : $INFRA_ID"
echo "  apiDomain    : $API_DOMAIN"
echo "  appsDomain   : $APPS_DOMAIN"
echo "  region       : $REGION"
echo "  az           : $AZ"
echo "  ami          : $AMI"
echo "  guid         : $GUID"
echo "  uuid         : $UUID"
echo "  hostedZoneID : $HOSTED_ZONE_ID"
echo "  accessKeyID  : $AWS_ACCESS_KEY_ID"


# ── Step 3: Update values.yaml ────────────────────────────────────────────────
echo ""
info "Updating bootstrap/values.yaml..."

# Backup original
cp "$VALUES_FILE" "${VALUES_FILE}.bak"

yq -i ".cluster.infraID           = \"$INFRA_ID\""                               "$VALUES_FILE"
yq -i ".cluster.apiUrl            = \"$API_URL\""                                "$VALUES_FILE"
yq -i ".cluster.apiDomain         = \"$API_DOMAIN\""                             "$VALUES_FILE"
yq -i ".cluster.domain            = \"$APPS_DOMAIN\""                            "$VALUES_FILE"
yq -i ".cluster.region            = \"$REGION\""                                 "$VALUES_FILE"
yq -i ".aws.ami                   = \"$AMI\""                                    "$VALUES_FILE"
yq -i ".aws.guid                  = \"$GUID\""                                   "$VALUES_FILE"
yq -i ".aws.uuid                  = \"$UUID\""                                   "$VALUES_FILE"
yq -i ".aws.az                    = \"$AZ\""                                     "$VALUES_FILE"
yq -i ".deployer.domain           = \"$APPS_DOMAIN\""                            "$VALUES_FILE"
yq -i ".certManager.route53.accessKeyID  = \"$AWS_ACCESS_KEY_ID\""              "$VALUES_FILE"
yq -i ".certManager.route53.hostedZoneID = \"$HOSTED_ZONE_ID\""                 "$VALUES_FILE"
yq -i ".certManager.dnsZones       = [\"$APPS_DOMAIN\", \"$API_DOMAIN\"]"       "$VALUES_FILE"

success "bootstrap/values.yaml updated  (backup saved as values.yaml.bak)"

# ── Step 4: Create cert-manager-aws-creds secret ──────────────────────────────
echo ""
info "Creating cert-manager-aws-creds secret on cluster..."

# cert-manager namespace may not exist yet — create it if missing
oc get namespace cert-manager &>/dev/null || oc create namespace cert-manager

if oc get secret cert-manager-aws-creds -n cert-manager &>/dev/null; then
  warn "cert-manager-aws-creds already exists — skipping (delete and re-run to overwrite)"
else
  oc create secret generic cert-manager-aws-creds -n cert-manager \
    --from-literal=aws_secret_access_key="$AWS_SECRET_ACCESS_KEY"
  success "cert-manager-aws-creds secret created"
fi


# ── Step 5: Summary ───────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Configuration complete!                   ${NC}"
echo -e "${GREEN}════════════════════════════════════════════${NC}"
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo ""
echo "  1. Review the updated values file:"
echo "       cat bootstrap/values.yaml"
echo ""
echo "  2. Commit and push to GitHub:"
echo "       git add bootstrap/values.yaml"
echo "       git commit -m \"config: update values for new cluster ($INFRA_ID)\""
echo "       git push"
echo ""
echo "  3. Install the GitOps operator:"
echo "       oc apply -f setup/gitops-subscription.yaml"
echo ""
echo "  4. Wait for it to be ready (~2 min):"
echo "       oc wait csv -n openshift-gitops \\"
echo "         --for=jsonpath='{.status.phase}'=Succeeded \\"
echo "         -l operators.coreos.com/openshift-gitops-operator.openshift-gitops \\"
echo "         --timeout=300s"
echo ""
echo "  5. Grant ArgoCD cluster-admin:"
echo "       oc apply -f setup/cluster-admin-binding.yaml"
echo ""
echo "  6. Deploy the bootstrap Application:"
echo "       oc apply -f setup/bootstrap.yaml"
echo ""
echo "  7. Monitor progress:"
echo "       oc get applications -n openshift-gitops -w"
echo ""
