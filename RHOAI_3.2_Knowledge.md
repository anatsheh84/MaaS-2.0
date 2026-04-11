# RHOAI 3.2 — MaaS & GenAI Playground Knowledge Base

## Purpose

This document captures verified findings from hands-on investigation of the Red Hat OpenShift AI (RHOAI) 3.2 platform, specifically the Models-as-a-Service (MaaS) architecture and GenAI Playground. It covers both documented behavior and undocumented internals discovered through cluster inspection.

**Cluster:** RHPDS sandbox, RHOAI 3.2 (Developer Preview for MaaS)
**Date of investigation:** April 2026

---

## 1. MaaS Architecture Overview

### Components

| Component | Namespace | Role |
|---|---|---|
| **maas-api** | `maas-api` | Tier lookup, model discovery, user-to-tier mapping |
| **Kuadrant (Authorino + Limitador)** | `openshift-ingress` | Auth policy enforcement + rate limiting on the gateway |
| **maas-default-gateway** | `openshift-ingress` | Istio/Envoy gateway for model inference traffic. External LoadBalancer. |
| **data-science-gateway** | `openshift-ingress` | Internal ClusterIP gateway for OpenShift AI dashboard traffic. Has OAuth auth filter. |
| **kube-auth-proxy** | `openshift-ingress` | OAuth2 proxy that authenticates dashboard users via OpenShift OAuth |
| **KServe + LLMInferenceService** | `llm` | Model serving infrastructure (vLLM pods) |
| **LlamaStack** | `wksp-*` / user namespaces | Per-user RAG/inference orchestration layer |
| **gen-ai-ui** | `redhat-ods-applications` (in `rhods-dashboard` pod) | Proxy that injects user OAuth tokens into LlamaStack requests |

### Gateways

Two separate gateways serve different purposes:

```
┌─────────────────────────────────────────────────────────────────┐
│  maas-default-gateway (LoadBalancer, external)                  │
│  - Serves: /llm/<model>/v1/* (model inference)                 │
│            /v1/models (model discovery via maas-api)            │
│            /maas-api/* (admin endpoints)                        │
│  - Auth: Kuadrant AuthPolicy (kubernetesTokenReview)            │
│  - Rate limits: RateLimitPolicy + TokenRateLimitPolicy          │
│  - EnvoyFilters: kuadrant-auth, kuadrant-ratelimiting           │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  data-science-gateway (ClusterIP, internal only)                │
│  - Serves: OpenShift AI dashboard, OAuth callback               │
│  - Auth: data-science-authn-filter EnvoyFilter                  │
│    → ext_authz → kube-auth-proxy (OAuth2 proxy)                 │
│    → Lua script replaces Authorization header with user token   │
│  - NOT used for model inference directly                        │
└─────────────────────────────────────────────────────────────────┘
```


---

## 2. Tier System

### Tier Definitions

Tiers are defined in the `tier-to-group-mapping` ConfigMap in the `maas-api` namespace (documentation incorrectly says `redhat-ods-applications`).

```yaml
# ConfigMap: tier-to-group-mapping (namespace: maas-api)
tiers:
  - name: free
    displayName: Free Tier
    level: 0
    groups:
      - tier-free-users
      - system:authenticated    # All authenticated users default to free

  - name: premium
    displayName: Premium Tier
    level: 1
    groups:
      - tier-premium-users
      - premium-group

  - name: enterprise
    displayName: Enterprise Tier
    level: 2
    groups:
      - tier-enterprise-users
      - enterprise-group
      - admin-group
```

**Rule:** Users are assigned to the tier with the highest `level` among their matching groups.

### Tier Namespaces and Service Accounts

MaaS auto-creates tier namespaces with per-user ServiceAccounts:

| Namespace | SAs (user-facing) | Purpose |
|---|---|---|
| `maas-default-gateway-tier-free` | `kube-admin-81378af5` | Free-tier API access tokens |
| `maas-default-gateway-tier-premium` | `user1-b3daa77b`, `user2-a1881c06` | Premium-tier API access tokens |
| `maas-default-gateway-tier-enterprise` | `llamastack-internal` | Enterprise-tier (used by LlamaStack backend) |


### User-to-Tier Mapping (Verified)

| User | OpenShift Groups | Resolved Tier |
|---|---|---|
| `admin` (kube:admin) | `system:authenticated`, `tier-enterprise-users`, `litemaas-admins` | **Enterprise** (level 2) |
| `user1` | `system:authenticated`, `tier-premium-users` | **Premium** (level 1) |
| `user2` | `system:authenticated`, `tier-premium-users` | **Premium** (level 1) |
| Any other authenticated user | `system:authenticated` | **Free** (level 0) |

**Note:** The SA namespace does NOT determine the tier. SA `kube-admin-81378af5` is in `tier-free` namespace (likely created before admin was added to enterprise group), but the AuthPolicy resolves tier via the maas-api `/v1/tiers/lookup` endpoint using the SA's groups.

### Rate Limits

Two policies enforce limits on the `maas-default-gateway`:

**RateLimitPolicy** (request count):

| Tier | Limit | Window |
|---|---|---|
| Free | 5 requests | 2 minutes |
| Premium | 20 requests | 2 minutes |
| Enterprise | (no limit defined) | — |

**TokenRateLimitPolicy** (token consumption):

| Tier | Limit | Window |
|---|---|---|
| Free | 100 tokens | 1 minute |
| Premium | 50,000 tokens | 1 minute |
| Enterprise | 100,000 tokens | 1 minute |

### 429 Response Format (v3.2)

When a user exceeds their tier quota:

- **HTTP Status:** 429
- **Content-Type:** `text/plain`
- **Body:** `Too Many Requests`
- **No structured headers** — `X-RateLimit-Remaining`, `Retry-After`, and `quota_error` JSON body are NOT present in v3.2. These may be v3.3/3.4 features.
- **No rate limit headers on 200 responses** — successful responses have no `X-RateLimit-*` headers.


---

## 3. Token Flow — GenAI Playground (Dashboard)

### Complete Verified Flow

```
Browser (OpenShift AI Dashboard)
    │
    │ (1) User authenticates via OpenShift OAuth
    ▼
data-science-gateway (Envoy, ClusterIP)
    │
    │ (2) data-science-authn-filter EnvoyFilter:
    │     → ext_authz calls kube-auth-proxy (OAuth2 proxy)
    │     → Validates session cookie
    │     → Returns x-auth-request-access-token header
    │
    │ (3) Lua script in the EnvoyFilter:
    │     → Extracts x-auth-request-access-token
    │     → REPLACES Authorization header: "Bearer <user-oauth-token>"
    │     → Adds x-forwarded-access-token header
    │     → Strips OAuth2 proxy cookies before forwarding upstream
    │
    ▼
rhods-dashboard pod (redhat-ods-applications)
    │
    │ (4) gen-ai-ui container (port 8143):
    │     Args: --auth-method=user_token
    │           --auth-token-header=x-forwarded-access-token
    │     → Receives user's OAuth token via x-forwarded-access-token
    │     → Proxies requests to LlamaStack
    │     → Injects token via: X-LlamaStack-Provider-Data: {"vllm_api_token": "<user-token>"}
    │
    ▼
LlamaStack pod (wksp-* namespace, port 8321)
    │
    │ (5) LlamaStack request_provider_data_context():
    │     → Parses X-LlamaStack-Provider-Data header (JSON)
    │     → _get_api_key_from_config_or_provider_data():
    │       IF provider_data has "vllm_api_token" → OVERRIDES config api_token ("fake")
    │       ELSE → uses config api_token (defaults to "fake" → will get 401)
    │
    │ (6) LlamaStack's remote::vllm provider:
    │     → Creates AsyncOpenAI client with the provider-data token
    │     → Calls: http://maas.apps.<cluster>/llm/<model>/v1/chat/completions
    │     → Authorization: Bearer <user-token> (the REAL token, not "fake")
    │
    ▼
maas-default-gateway (Envoy, LoadBalancer)
    │
    │ (7) Kuadrant AuthPolicy:
    │     → kubernetesTokenReview (audience: maas-default-gateway-sa)
    │     → Extracts user identity and groups
    │     → Calls maas-api /v1/tiers/lookup to resolve tier
    │     → Sets auth.identity.tier and auth.identity.userid
    │
    │ (8) Kuadrant RateLimitPolicy + TokenRateLimitPolicy:
    │     → Checks request count and token count against tier limits
    │     → If exceeded → 429 Too Many Requests
    │
    ▼
KServe HTTPRoute → vLLM pod (llm namespace)
    │
    │ (9) vLLM processes inference request
    │     → Returns chat completion response
    │
    ▼
Response flows back through the chain to the browser
```

### Key Proof Points

1. **Token override mechanism verified:** Sending `X-LlamaStack-Provider-Data: {"vllm_api_token": "<real-token>"}` to LlamaStack successfully overrides the `fake` config token and enables inference. Without this header, LlamaStack fails with HTTP 500 (wrapping a 401 from the gateway).

2. **Code path in LlamaStack:**
   - `llama_stack/providers/remote/inference/vllm/vllm.py` line 36: `provider_data_api_key_field = "vllm_api_token"`
   - `llama_stack/providers/utils/inference/openai_mixin.py` line 180-188: `_get_api_key_from_config_or_provider_data()` checks provider data first, overrides config key
   - `llama_stack/core/request_headers.py`: `parse_request_provider_data()` parses `X-LlamaStack-Provider-Data` JSON header

3. **Rate limits apply through this path:** Using a free-tier token via provider data results in 429 after 5 requests or 100 tokens.


---

## 4. Token Flow — Direct API Access

```
API Client (curl, SDK)
    │
    │ (1) User generates SA token:
    │     oc create token <sa-name> -n <tier-namespace> \
    │       --audience=maas-default-gateway-sa --duration=600s
    │
    │ (2) Sends request with: Authorization: Bearer <sa-token>
    │
    ▼
maas-default-gateway (Envoy, LoadBalancer)
    │
    │ (3) Same Kuadrant auth + rate limit flow as step 7-8 above
    │
    ▼
KServe HTTPRoute → vLLM pod
```

### Token Creation Example

```bash
# Create a free-tier token (minimum 600s / 10 minutes)
TOKEN=$(oc create token kube-admin-81378af5 \
  -n maas-default-gateway-tier-free \
  --audience=maas-default-gateway-sa \
  --duration=600s)

# Use it
curl -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-4b-instruct","messages":[{"role":"user","content":"hello"}],"max_tokens":10}' \
  "https://maas.apps.<cluster>/llm/qwen3-4b-instruct/v1/chat/completions" \
  --insecure
```

**Constraint:** Kubernetes enforces a minimum token duration of 10 minutes (600s). Requests for shorter durations fail with `spec.expirationSeconds: Invalid value`.


---

## 5. Token Flow — Notebook App (Current Architecture)

Our custom notebook app (`charts/notebook-api/` + `charts/notebook-ui/`) currently bypasses per-user rate limiting:

```
Browser → oauth-proxy (OpenShift OAuth, sets X-Forwarded-User)
    → nginx → notebook-api (FastAPI)
        → LlamaStack (wksp-user1/user2, using enterprise-tier llamastack-vllm-token)
            → maas-default-gateway (enterprise SA token → NO rate limits)
                → vLLM
```

**Key difference from GenAI Playground:** Our notebook-api calls LlamaStack directly (not through the dashboard/gen-ai-ui proxy), so there's no `X-LlamaStack-Provider-Data` header injection. LlamaStack uses the `llamastack-vllm-token` secret, which contains the enterprise-tier `llamastack-internal` SA token.

### RBAC Infrastructure Already Exists

The notebook-api chart (`charts/notebook-api/templates/rbac.yaml`) already provisions RBAC to mint per-user tier tokens:

```yaml
# Role: notebook-api-token-requester (in each tier namespace)
rules:
  - apiGroups: [""]
    resources: ["serviceaccounts"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["serviceaccounts/token"]
    verbs: ["create"]
```

This allows the notebook-api SA (`maas-rag/notebook-api`) to:
1. List SAs in `maas-default-gateway-tier-free` and `maas-default-gateway-tier-premium`
2. Create short-lived tokens for those SAs with the `maas-default-gateway-sa` audience

**Not yet wired up in code** — the notebook-api currently doesn't use this capability.


---

## 6. maas-api Service

### Endpoints (Verified)

| Endpoint | Method | Auth Headers Required | Response |
|---|---|---|---|
| `/v1/tiers/lookup` | POST | None (internal only) | `{"tier":"free","displayName":"Free Tier"}` |
| `/v1/models` | GET | `X-MaaS-Username`, `X-MaaS-Group` | Array of model objects with readiness status |

### Endpoint Details

**`/v1/tiers/lookup`** — Resolves user groups to a tier. Called by the Kuadrant AuthPolicy as metadata enrichment.

```bash
curl -s http://maas-api.maas-api.svc.cluster.local:8080/v1/tiers/lookup \
  -H "Content-Type: application/json" \
  -d '{"groups":["system:authenticated","tier-premium-users"]}'
# → {"tier":"premium","displayName":"Premium Tier"}
```

**`/v1/models`** — Lists all LLMInferenceService models. The maas-api-auth-policy injects `X-MaaS-Username` and `X-MaaS-Group` headers after Kuadrant token review.

```bash
curl -s http://maas-api.maas-api.svc.cluster.local:8080/v1/models \
  -H "X-MaaS-Username: system:serviceaccount:..." \
  -H 'X-MaaS-Group: ["system:authenticated"]'
# → {"data": [{"id":"qwen3-4b-instruct","ready":true,...}, ...]}
```

### Endpoints NOT Found in v3.2

The following were probed and returned 404:
`/v1/tiers`, `/v1/tiers/me`, `/v1/users`, `/v1/keys`, `/v1/apikeys`, `/v1/tokens`, `/v1/usage`, `/v1/quota`, `/v1/health`, `/healthz`, `/readyz`

Binary string analysis of `/app/maas-api` (Go binary) shows internal references to `quota`, `usage`, `tokens`, `tiers` — these may become HTTP endpoints in v3.3/3.4.

### maas-api AuthPolicy

The `maas-api-auth-policy` is separate from the main `gateway-auth-policy`:

- Targets: `HTTPRoute/maas-api-route`
- Accepts audiences: `https://kubernetes.default.svc` AND `maas-default-gateway-sa`
- Injects response headers:
  - `X-MaaS-Username` ← `auth.identity.user.username`
  - `X-MaaS-Group` ← `auth.identity.user.groups.@tostr`


---

## 7. GenAI Playground — Auto-Provisioning

### What RHOAI Auto-Creates

When a user creates a **Data Science Project** from the OpenShift AI dashboard:
- Only a namespace with label `opendatahub.io/dashboard=true` is created
- No LlamaStack, no tokens, no pods

When a user creates a **GenAI Playground** from the OpenShift AI dashboard:
- `LlamaStackDistribution` CR (`lsd-genai-playground`)
- `llama-stack-config` ConfigMap — auto-generated with:
  - Only 1 model (first available LLMInferenceService)
  - `api_token: ${env.VLLM_API_TOKEN_1:=fake}` (defaults to literal "fake")
  - `inline::sentence-transformers` embedding provider (runs in-pod, uses `ibm-granite/granite-embedding-125m-english`)
- `lsd-genai-playground-sa` ServiceAccount + anyuid RoleBinding
- Deployment, Service, ReplicaSet, Pod
- CA bundle ConfigMap

### What RHOAI Does NOT Auto-Create

- `llamastack-vllm-token` secret — NOT auto-provisioned
- `VLLM_API_TOKEN_1` env var is set to literal string `fake`, NOT a secretKeyRef
- No per-user tier token injection at the LlamaStack level

### How the Playground Works Despite `fake` Token

The playground UI runs inside the OpenShift AI dashboard (`rhods-dashboard` pod). The `gen-ai-ui` sidecar container proxies requests to LlamaStack and injects the user's real OAuth token via the `X-LlamaStack-Provider-Data` header. See Section 3 for the complete flow.

### Comparison: Our Notebook vs GenAI Playground

| Aspect | GenAI Playground | Our Notebook App |
|---|---|---|
| Token source | User's OAuth token injected by `gen-ai-ui` | Shared enterprise SA token from `llamastack-vllm-token` secret |
| Rate limits apply? | Yes — user's tier | No — enterprise tier (unlimited) |
| Token injection mechanism | `X-LlamaStack-Provider-Data: {"vllm_api_token": "<token>"}` | `VLLM_API_TOKEN_1` env var from secret |
| Per-user isolation | Yes (per-user workspace namespace + PVC) | Yes (per-user LlamaStack pod + PVC) |
| Models available | 1 (auto-detected) | All registered in custom ConfigMap |


---

## 8. data-science-authn-filter EnvoyFilter (Full Specification)

This EnvoyFilter applies to `data-science-gateway` workloads and has three patches:

### Patch 1: ext_authz Filter

Inserts an external authorization filter that calls `kube-auth-proxy`:

- **Server URI:** `https://kube-auth-proxy.openshift-ingress.svc.cluster.local:8443/oauth2/auth`
- **Cluster:** `kube-auth-proxy` (TLS, STRICT_DNS)
- **Allowed request headers to auth proxy:** `cookie`
- **Allowed response headers from auth proxy to upstream:**
  - `x-auth-request-user`
  - `x-auth-request-email`
  - `x-auth-request-access-token`
- **Allowed response headers to client:** `set-cookie`

### Patch 2: Lua Script

Runs after ext_authz. Key behavior:

```lua
function envoy_on_request(request_handle)
  local access_token = request_handle:headers():get("x-auth-request-access-token")
  if access_token then
    -- Inject token for upstream
    request_handle:headers():add("x-forwarded-access-token", access_token)
    -- REPLACE Authorization header (overrides whatever was there)
    request_handle:headers():replace("authorization", "Bearer " .. access_token)
    -- Strip OAuth2 proxy cookies (pattern: _oauth2_proxy*)
    -- ... cookie filtering logic ...
  end
end
```

### Patch 3: kube-auth-proxy Cluster Definition

Adds the upstream cluster for the auth proxy:
- Address: `kube-auth-proxy.openshift-ingress.svc.cluster.local:8443`
- TLS with service CA validation
- 5s connect timeout


---

## 9. Kuadrant Policies (Complete)

### AuthPolicy: gateway-auth-policy

- **Target:** `maas-default-gateway`
- **Authentication:** `kubernetesTokenReview` with audience `maas-default-gateway-sa`
- **Identity normalization:** Extracts short username from SA: `auth.identity.user.username.split(":")[3]`
- **Metadata enrichment:** Calls `maas-api /v1/tiers/lookup` with user groups to resolve tier
- **Authorization:** `kubernetesSubjectAccessReview` — checks if user can `POST` to `llminferenceservices` in the target namespace
- **Response:** Injects `auth.identity.userid` and `auth.identity.tier` into the request context
- **Caching:**
  - Auth token cache: 600s TTL, keyed by Authorization header
  - Tier lookup cache: 300s TTL, keyed by username
  - Authorization cache: 60s TTL, keyed by `{username}:{path}`

### AuthPolicy: maas-api-auth-policy

- **Target:** `HTTPRoute/maas-api-route` (in `maas-api` namespace)
- **Authentication:** `kubernetesTokenReview` with audiences: `https://kubernetes.default.svc` AND `maas-default-gateway-sa`
- **Response headers:** `X-MaaS-Username`, `X-MaaS-Group`

### RateLimitPolicy: gateway-rate-limits

- **Target:** `maas-default-gateway`
- **Counters:** Per `auth.identity.userid`
- **Limits:**
  - `free`: 5 requests / 2 minutes (when `auth.identity.tier == "free"`)
  - `premium`: 20 requests / 2 minutes (when `auth.identity.tier == "premium"`)
  - Enterprise: no request rate limit defined

### TokenRateLimitPolicy: gateway-token-rate-limits

- **Target:** `maas-default-gateway`
- **Counters:** Per `auth.identity.userid`
- **Limits:**
  - `free`: 100 tokens / 1 minute
  - `premium`: 50,000 tokens / 1 minute
  - `enterprise`: 100,000 tokens / 1 minute


---

## 10. Testing Methodology

### Test 1: Tier Lookup Validation

```bash
# Direct call to maas-api tier lookup service
oc exec -n maas-api deployment/maas-api -- curl -s \
  http://localhost:8080/v1/tiers/lookup \
  -H "Content-Type: application/json" \
  -d '{"groups":["system:authenticated"]}'
# Result: {"tier":"free","displayName":"Free Tier"}

oc exec -n maas-api deployment/maas-api -- curl -s \
  http://localhost:8080/v1/tiers/lookup \
  -H "Content-Type: application/json" \
  -d '{"groups":["system:authenticated","tier-premium-users"]}'
# Result: {"tier":"premium","displayName":"Premium Tier"}

oc exec -n maas-api deployment/maas-api -- curl -s \
  http://localhost:8080/v1/tiers/lookup \
  -H "Content-Type: application/json" \
  -d '{"groups":["system:authenticated","admin-group"]}'
# Result: {"tier":"enterprise","displayName":"Enterprise Tier"}
```

**Finding:** Highest-level tier wins when user belongs to multiple groups.

### Test 2: Token Creation for Tier SAs

```bash
# Create a short-lived token for the free-tier SA
# IMPORTANT: Minimum duration is 600s (10 minutes)
TOKEN=$(oc create token kube-admin-81378af5 \
  -n maas-default-gateway-tier-free \
  --audience=maas-default-gateway-sa \
  --duration=600s)

# Verify the token payload
echo "$TOKEN" | python3 -c "
import sys, json, base64
token = sys.stdin.read().strip()
payload = token.split('.')[1]
payload += '=' * (4 - len(payload) % 4)
data = json.loads(base64.urlsafe_b64decode(payload))
print(json.dumps(data, indent=2))
"
# Confirms: sub=system:serviceaccount:maas-default-gateway-tier-free:kube-admin-81378af5
#           aud=["maas-default-gateway-sa"]
```

**Finding:** Token creation works. Minimum 600s duration enforced by Kubernetes.


### Test 3: Request Rate Limiting (Free Tier)

```bash
FREE_TOKEN=$(oc create token kube-admin-81378af5 \
  -n maas-default-gateway-tier-free \
  --audience=maas-default-gateway-sa --duration=600s)

# Send 7 rapid requests (free limit = 5/2min)
for i in $(seq 1 7); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $FREE_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"model":"qwen3-4b-instruct","messages":[{"role":"user","content":"hi"}],"max_tokens":3}' \
    "https://maas.apps.<cluster>/llm/qwen3-4b-instruct/v1/chat/completions" --insecure)
  echo "Request $i: HTTP $STATUS"
done
```

**Result:** Requests 1-5 return HTTP 200. Requests 6-7 return HTTP 429.

### Test 4: Token Rate Limiting (Free Tier)

```bash
# Request with max_tokens=80 (free limit = 100 tokens/min)
# Request 1: Consumes ~95 tokens (15 prompt + 80 completion) → 200
# Request 2: Would exceed 100 token limit → 429
```

**Result:** First request uses 95 tokens (200 OK). Second request immediately returns 429.

### Test 5: X-LlamaStack-Provider-Data Token Override

```bash
# WITHOUT provider data — LlamaStack uses "fake" token → 500 (wrapping 401)
oc exec <llamastack-pod> -n mydsproject -- curl -s \
  -H "Content-Type: application/json" \
  -d '{"model":"maas-vllm-inference-1/qwen3-4b-instruct","input":"hi","max_tokens":5}' \
  "http://localhost:8321/v1/responses"
# Result: {"detail":"Internal server error"} HTTP 500

# WITH provider data — real token overrides "fake" → 200
ENT_TOKEN=$(oc get secret llamastack-vllm-token -n wksp-user1 -o jsonpath='{.data.token}' | base64 -d)
oc exec <llamastack-pod> -n mydsproject -- curl -s \
  -H "Content-Type: application/json" \
  -H "X-LlamaStack-Provider-Data: {\"vllm_api_token\": \"$ENT_TOKEN\"}" \
  -d '{"model":"maas-vllm-inference-1/qwen3-4b-instruct","input":"hi","max_tokens":5}' \
  "http://localhost:8321/v1/responses"
# Result: {"status":"completed","output":[...]} HTTP 200
```

**Finding:** This is the definitive proof that `X-LlamaStack-Provider-Data` is the mechanism used by the GenAI Playground to inject per-user tokens.

### Test 6: GenAI Playground with Default Config

Created a fresh Data Science Project (`mydsproject`) and GenAI Playground from the OpenShift AI dashboard:

1. **Namespace creation** — only default OpenShift resources (SAs, rolebindings, CA configmaps). No LlamaStack.
2. **Playground creation** — auto-deploys LlamaStackDistribution, ConfigMap (1 model, `api_token=fake`), SA, Deployment.
3. **Token secret NOT created** — `llamastack-vllm-token` is absent. `VLLM_API_TOKEN_1=fake` (literal).
4. **LlamaStack logs:** `Error code: 401` on `list_provider_model_ids()` at startup (model refresh fails).
5. **Yet chat works** — because the `gen-ai-ui` proxy injects the user's real token via provider data header.


---

## 11. Documentation vs Reality (v3.2)

| Topic | Documentation Says | Cluster Reality |
|---|---|---|
| `tier-to-group-mapping` location | `redhat-ods-applications` namespace | Actually in `maas-api` namespace |
| 429 response format | `X-RateLimit-Remaining`, `Retry-After`, `quota_error` JSON | Plain text `Too Many Requests`, no structured headers |
| Rate limit headers on 200 | Implied present | Not present in v3.2 |
| GenAI Playground token flow | Not documented | `gen-ai-ui` → `X-LlamaStack-Provider-Data` → token override |
| `gen-ai-ui` component | Not documented | Sidecar in `rhods-dashboard` pod, proxies with `--auth-method=user_token` |
| Data Science Project creation | Not detailed | Only creates namespace with labels, nothing else |
| Playground auto-provisioning | Not detailed | LlamaStack + ConfigMap auto-created, but token defaults to `fake` |
| Red Hat Connectivity Link role | "Required dependency" for distributed inference/guardrails | Exact role in token flow unclear — may handle some internal routing |
| MaaS API key generation | Users generate from dashboard | No API key endpoint found in v3.2 maas-api |
| Llama Stack OAuth/RBAC | `claims_mapping` + role-based access policies | Not observed in v3.2 (may be v3.3+ feature) |

---

## 12. Implications for Notebook App Features

### Feature 1: User Icon + Logout
- **Source of identity:** `X-Forwarded-User` header from oauth-proxy sidecar
- **Logout URL:** `/oauth/sign_out` (oauth-proxy standard endpoint)
- **Effort:** 1-2h, pure UI + thin `/whoami` API endpoint

### Feature 2: User Tier Display
- **Approach:** notebook-api can call `maas-api /v1/tiers/lookup` with user's groups
- **Getting user groups:** Query Kubernetes API for user's group membership, or read from `X-Forwarded-Groups` if oauth-proxy passes them
- **Fallback:** Hardcode tier info from ConfigMap and look up user's SA in tier namespaces
- **Effort:** 2-3h

### Feature 3: Usage / Remaining Balance
- **v3.2 limitation:** No `X-RateLimit-Remaining` headers, no usage/quota API endpoints
- **Two paths:**
  - **App-level tracking** (recommended): Count requests/tokens in notebook-api per user session. Display against known tier limits.
  - **Per-user tier enforcement** (better long-term): Use `X-LlamaStack-Provider-Data` to pass per-user tier tokens. Then track via app-level counters since gateway doesn't return remaining counts in v3.2.
- **RBAC already exists:** `notebook-api-token-requester` Role/RoleBinding in tier namespaces. notebook-api can mint per-user SA tokens.
- **Effort:** 4-6h (includes wiring up per-user token injection)

### Feature 4: RedBook Branding
- Pure UI change. Replace title, add logo SVG, adjust CSS colors.
- **Effort:** 1h


---

## 13. Kubernetes Resources Reference

### All MaaS-Related Namespaces

```
maas-api                               — maas-api service (tier lookup, model discovery)
maas-default-gateway-tier-free         — Free tier SAs
maas-default-gateway-tier-premium      — Premium tier SAs
maas-default-gateway-tier-enterprise   — Enterprise tier SA (llamastack-internal)
llm                                    — Model serving (LLMInferenceService, vLLM pods)
openshift-ingress                      — Gateways, Kuadrant policies, kube-auth-proxy
redhat-ods-applications                — RHOAI dashboard, LlamaStack operator, notebook controller
wksp-user1 / wksp-user2               — Per-user workspaces (LlamaStack + PVC)
maas-rag                               — Custom notebook app (notebook-api + notebook-ui)
```

### CRDs in Play

| CRD | API Group | Purpose |
|---|---|---|
| `LLMInferenceService` | `serving.kserve.io/v1alpha1` | Model deployment on KServe/vLLM |
| `LlamaStackDistribution` | `llamastack.io/v1alpha1` | LlamaStack instance management |
| `Gateway` | `gateway.networking.k8s.io/v1` | Envoy/Istio gateways |
| `HTTPRoute` | `gateway.networking.k8s.io/v1` | Traffic routing rules |
| `AuthPolicy` | `kuadrant.io/v1` | Authentication/authorization policies |
| `RateLimitPolicy` | `kuadrant.io/v1` | Request rate limiting |
| `TokenRateLimitPolicy` | `kuadrant.io/v1alpha1` | Token consumption rate limiting |
| `GatewayConfig` | `services.platform.opendatahub.io/v1alpha1` | OIDC auth configuration for data-science-gateway |
| `EnvoyFilter` | `networking.istio.io/v1alpha3` | Custom Envoy filter injection |

### OpenShift Groups

```
tier-enterprise-users   — admin
tier-premium-users      — user1, user2
litemaas-admins         — admin
litemaas-readonly       — (empty)
litemaas-users          — (empty)
maas-registry-users     — (empty)
rhods-admins            — (empty)
```
