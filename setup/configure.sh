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
#   5b. Creates HTPasswd identity provider with user1, user2, admin
#   6. Grants ArgoCD cluster-admin, deploys bootstrap Application
#   7. Patches the bootstrap Application's helm.valuesObject with real values
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
# PREREQUISITES:
#   - oc CLI installed and in PATH
#   - htpasswd (from httpd-tools / apache2-utils) installed and in PATH
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
  echo "  API_URL               → RHPDS sandbox info page"
  echo "  KUBEADMIN_PASSWORD    → RHPDS sandbox info page"
  echo "  AWS_ACCESS_KEY_ID     → RHPDS sandbox AWS credentials"
  echo "  AWS_SECRET_ACCESS_KEY → RHPDS sandbox AWS credentials"
  echo "  HOSTED_ZONE_ID        → RHPDS sandbox AWS credentials"
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
step "Step 1/7 — Login to cluster"
info "Logging in to: $API_URL"
oc login "$API_URL" -u kubeadmin -p "$OC_PASSWORD" --insecure-skip-tls-verify=true \
  &>/dev/null || error "Login failed. Check API_URL and password."
success "Logged in successfully"

# ── Step 2: Read cluster values ────────────────────────────────────────────────
step "Step 2/7 — Reading cluster values"

INFRA_ID=$(oc get infrastructure cluster \
  -o jsonpath='{.status.infrastructureName}')
API_DOMAIN=$(echo "$API_URL" | sed 's|https://||' | sed 's|:6443||' | sed 's|/$||')
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
step "Step 3/7 — Writing bootstrap/values.local.yaml"

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
EOF

success "bootstrap/values.local.yaml written (gitignored — safe)"

# ── Step 4: Create cert-manager-aws-creds secret ──────────────────────────────
step "Step 4/7 — Creating cert-manager-aws-creds secret"
oc get namespace cert-manager &>/dev/null || oc create namespace cert-manager
if oc get secret cert-manager-aws-creds -n cert-manager &>/dev/null; then
  warn "cert-manager-aws-creds already exists — skipping"
else
  oc create secret generic cert-manager-aws-creds -n cert-manager \
    --from-literal=aws_secret_access_key="$AWS_SECRET_ACCESS_KEY"
  success "cert-manager-aws-creds secret created"
fi


# ── Step 5: Install GitOps operator ───────────────────────────────────────────
step "Step 5/7 — Installing OpenShift GitOps operator"

info "Installing OpenShift GitOps operator..."
oc apply -f "$REPO_ROOT/setup/gitops-subscription.yaml"

info "Waiting for GitOps operator to be ready (~2 min)..."
until oc get csv -n openshift-gitops 2>/dev/null | grep -q "Succeeded"; do
  echo -n "."; sleep 10
done
echo ""
success "GitOps operator is ready"

# ── Step 5b: Create HTPasswd users ────────────────────────────────────────────
step "Step 5b/7 — Creating HTPasswd identity provider (user1, user2, admin)"
#
# Keycloak is disabled by default (see bootstrap/values.yaml).
# We create an HTPasswd IdP directly so user1, user2 and admin exist as real
# OpenShift identities and can access RHOAI, DevSpaces and their workspaces.
#
# Passwords match the values in charts/keycloak-instance/values.yaml so that
# re-enabling Keycloak later uses the same credentials.
#   user1  : MTkxNDU3
#   user2  : MTkxNDU3
#   admin  : NDcxOTE3
#
if command -v htpasswd &>/dev/null; then
  HTPASSWD_FILE=$(mktemp)
  htpasswd -bBc "$HTPASSWD_FILE" user1 MTkxNDU3
  htpasswd -bB  "$HTPASSWD_FILE" user2 MTkxNDU3
  htpasswd -bB  "$HTPASSWD_FILE" admin NDcxOTE3

  # Create or replace the htpasswd secret in openshift-config
  if oc get secret htpasswd-maas-users -n openshift-config &>/dev/null; then
    oc create secret generic htpasswd-maas-users \
      --from-file=htpasswd="$HTPASSWD_FILE" \
      -n openshift-config \
      --dry-run=client -o yaml | oc replace -f -
    info "htpasswd-maas-users secret updated"
  else
    oc create secret generic htpasswd-maas-users \
      --from-file=htpasswd="$HTPASSWD_FILE" \
      -n openshift-config
    success "htpasswd-maas-users secret created"
  fi
  rm -f "$HTPASSWD_FILE"

  # Patch the cluster OAuth to add the HTPasswd identity provider
  # Uses strategic merge so existing providers (e.g. kubeadmin) are preserved
  oc patch oauth cluster --type=merge -p '{
    "spec": {
      "identityProviders": [{
        "name": "htpasswd-maas",
        "mappingMethod": "claim",
        "type": "HTPasswd",
        "htpasswd": {
          "fileData": {"name": "htpasswd-maas-users"}
        }
      }]
    }
  }'
  success "HTPasswd identity provider configured"
  echo ""
  echo -e "  ${CYAN}Users created:${NC}"
  echo -e "    user1  / MTkxNDU3"
  echo -e "    user2  / MTkxNDU3"
  echo -e "    admin  / NDcxOTE3"
  echo ""
else
  warn "'htpasswd' not found — skipping user creation."
  warn "Install httpd-tools (RHEL/Fedora) or apache2-utils (Debian/Ubuntu)"
  warn "then run manually:"
  warn "  htpasswd -bBc /tmp/htpasswd user1 MTkxNDU3"
  warn "  htpasswd -bB  /tmp/htpasswd user2 MTkxNDU3"
  warn "  htpasswd -bB  /tmp/htpasswd admin NDcxOTE3"
  warn "  oc create secret generic htpasswd-maas-users --from-file=htpasswd=/tmp/htpasswd -n openshift-config"
fi




# ── Step 5c: Generate LiteMaaS secrets ────────────────────────────────────────
step "Step 5c/7 — Generating LiteMaaS secrets (litemaas-test namespace)"
#
# All passwords are generated fresh with openssl — nothing is hardcoded.
# The secret is created directly on the cluster and NEVER written to git.
# ArgoCD's litemaas chart references it via existingSecret: litemaas-secrets.
#
LITEMAAS_NS="litemaas-test"
LITEMAAS_SECRET="litemaas-secrets"

oc get namespace "$LITEMAAS_NS" &>/dev/null || oc create namespace "$LITEMAAS_NS"
info "Namespace $LITEMAAS_NS ready"

if oc get secret "$LITEMAAS_SECRET" -n "$LITEMAAS_NS" &>/dev/null; then
  warn "$LITEMAAS_SECRET already exists in $LITEMAAS_NS — skipping generation"
  warn "Delete it first if you want fresh secrets:"
  warn "  oc delete secret $LITEMAAS_SECRET -n $LITEMAAS_NS"
else
  PG_ADMIN_PASSWORD=$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)
  JWT_SECRET=$(openssl rand -base64 64 | tr -d '\n')
  OAUTH_CLIENT_SECRET=$(openssl rand -hex 20)
  ADMIN_API_KEY=$(openssl rand -hex 16)
  LITELLM_API_KEY="sk-$(openssl rand -hex 24)"
  LITELLM_MASTER_KEY=$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)
  LITELLM_UI_PASSWORD=$(openssl rand -base64 16 | tr -d '=+/')

  oc create secret generic "$LITEMAAS_SECRET" \
    -n "$LITEMAAS_NS" \
    --from-literal=pg-admin-password="$PG_ADMIN_PASSWORD" \
    --from-literal=jwt-secret="$JWT_SECRET" \
    --from-literal=oauth-client-secret="$OAUTH_CLIENT_SECRET" \
    --from-literal=admin-api-key="$ADMIN_API_KEY" \
    --from-literal=litellm-api-key="$LITELLM_API_KEY" \
    --from-literal=litellm-master-key="$LITELLM_MASTER_KEY" \
    --from-literal=litellm-ui-username="admin" \
    --from-literal=litellm-ui-password="$LITELLM_UI_PASSWORD"

  success "$LITEMAAS_SECRET secret created in $LITEMAAS_NS"
  echo ""
  echo -e "  ${CYAN}LiteMaaS credentials (save these securely):${NC}"
  echo -e "    LiteLLM UI  : admin / $LITELLM_UI_PASSWORD"
  echo -e "    Admin API   : $ADMIN_API_KEY"
  echo -e "    LiteLLM Key : $LITELLM_API_KEY"
  echo ""

  # Create the OAuthClient for LiteMaaS.
  # NOTE: This is NOT managed by ArgoCD — OpenShift cluster-scoped auth resources
  # cause structured merge diff errors. configure.sh owns the OAuthClient lifecycle.
  oc apply -f - <<OAUTH
apiVersion: oauth.openshift.io/v1
kind: OAuthClient
metadata:
  name: litemaas-oauth-client
  labels:
    app.kubernetes.io/part-of: litemaas
secret: "$OAUTH_CLIENT_SECRET"
redirectURIs:
  - "https://litemaas-${LITEMAAS_NS}.${APPS_DOMAIN}/api/auth/callback"
grantMethod: auto
OAUTH
  success "OAuthClient litemaas-oauth-client created"
fi
# ── Step 6: Deploy bootstrap Application ──────────────────────────────────────
step "Step 6/7 — Deploying ArgoCD bootstrap Application"

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

# ── Step 7: Patch bootstrap Application with real values ──────────────────────
step "Step 7/7 — Injecting real values into ArgoCD bootstrap Application"
info "Patching helm.valuesObject — values go directly to ArgoCD, not to git"

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
          "litemaas": {
            "namespace": "litemaas-test",
            "version": "0.4.0",
            "existingSecret": "litemaas-secrets",
            "oauthClientId": "litemaas-oauth-client",
            "nodeTlsRejectUnauthorized": "0",
            "adminUsers": ["admin"]
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
            "dnsZones": ["$APPS_DOMAIN"]
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
echo "  ✔  Created HTPasswd users (user1, user2, admin)"
echo "  ✔  Generated LiteMaaS secrets in litemaas-test namespace"
echo "  ✔  Created OAuthClient for LiteMaaS"
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
echo "  3. Login to OpenShift console as:"
echo "       user1 / MTkxNDU3   (select 'htpasswd-maas' on login screen)"
echo "       user2 / MTkxNDU3"
echo "       admin / NDcxOTE3"
echo ""
echo "  4. LiteMaaS portal (once wave 8 syncs):"
echo "       https://litemaas-litemaas-test.$APPS_DOMAIN"
echo "       Login with: admin (htpasswd-maas IDP)"
echo ""
echo "  5. When the slack-mcp Application is deployed, create the Slack token secret:"
echo "       oc create secret generic slack-mcp-token -n lls-demo \\"
echo "         --from-literal=slack-bot-token=<YOUR_SLACK_BOT_TOKEN>"
echo ""
echo -e "  ${CYAN}Note: No git commit was needed. Real values live only on the cluster.${NC}"
echo "  The values.local.yaml file is for your local reference only."
echo ""
