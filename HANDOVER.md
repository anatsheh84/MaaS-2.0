# MaaS 2.0 — Handover Prompt for New Chat Session

## Context

All actions can be done using Desktop Commander and the OpenShift `oc` command is installed and can be used after authentication.

You are continuing work on **MaaS 2.0** — a fully GitOps-driven Models-as-a-Service platform on OpenShift AI (RHOAI). The platform includes a **NotebookLM-style RAG application** built as a thin UI wrapper over LlamaStack. The key design principle is: **the notebook application is a thin wrapper — OpenShift AI with LlamaStack is the engine/core**. Do NOT add custom logic to the notebook-api; rely on LlamaStack's native APIs.

**Repo:** `github.com/anatsheh84/MaaS-2.0` | **Local clone:** `/Users/aelnatsh/Lab/MaaS`
**Cluster:** RHPDS sandbox cluster (use `--insecure-skip-tls-verify=true` for `oc login`)

---

## Architecture

```
User Browser → notebook-ui (React, PatternFly, nginx) → notebook-api (FastAPI)
                                                              ↓
                                                        LlamaStack v0.3.5.1 (rh-dev)
                                                         ├── Responses API (file_search + RAG)
                                                         ├── Files API (upload)
                                                         ├── Vector Stores API (notebooks)
                                                         └── remote::vllm providers
                                                              ├── maas-qwen3-4b-instruct → MaaS Gateway → Qwen vLLM
                                                              ├── maas-llama-3-1-8b-instruct-fp8 → MaaS Gateway → Llama vLLM
                                                              ├── maas-mistral-small-24b-fp8 → MaaS Gateway → Mistral vLLM
                                                              └── sentence-transformers → Snowflake Embed Service (GPU)
```

**Per-user isolation:** Each user gets their own LlamaStack pod + PVC (`wksp-user1`, `wksp-user2`). All data (sql_store.db, kvstore.db, milvus.db, raw files) is per-user on the PVC. The only shared component is the Snowflake embedding service (stateless, GPU, in `llm` namespace).

## Current Deployed Models (g6e.12xlarge, 4× NVIDIA L40S GPUs)

| Model | Parser | Chat Template | RAG Status |
|---|---|---|---|
| `qwen3-4b-instruct` | `--tool-call-parser=hermes` | Built-in (tokenizer_config.json) | ✅ Full RAG + citations |
| `llama-3-1-8b-instruct-fp8` | `--tool-call-parser=llama3_json` | `tool_chat_template_llama3.1_json.jinja` | ✅ Full RAG + citations |
| `mistral-small-24b-fp8` | `--tool-call-parser=mistral` | `tool_chat_template_mistral3.jinja` | ❌ Outputs raw JSON tool calls — see "Current Issue" |
| `phi-4-instruct-w8a8` | `--tool-call-parser=hermes` | None | Not registered in LlamaStack — deployed on cluster but not in ConfigMap models list |

**Embedding:** Snowflake Arctic Embed L v2.0 (1024-dim, 8192 context, multilingual) running as Deployment+Service in `llm` namespace via vLLM. Performance: 711 texts/sec GPU.

---

## Data-Driven LlamaStack ConfigMap

The ConfigMap template (`charts/llama-stack-instance/templates/configmap.yaml`) loops over a `models:` list in `values.yaml`. Each model gets its own `remote::vllm` provider with a dedicated MaaS gateway URL:

```yaml
# values.yaml
models:
  - name: qwen3-4b-instruct
    displayName: Qwen3 4B Instruct 2507
  - name: llama-3-1-8b-instruct-fp8
    displayName: Llama 3.1 8B Instruct FP8
  - name: mistral-small-24b-fp8
    displayName: Mistral Small 3.1 24B Instruct FP8
```

The template generates per-model providers and registered resources. All models show as "RAG enabled" in the UI dropdown. The notebook-api discovers models by querying **LlamaStack's `/v1/models` API** (not the Kubernetes API), so only models registered in the ConfigMap appear in the dropdown. The notebook-api maps UI model names to LlamaStack identifiers: `body.model` → `maas-{name}/{name}`.

---

## Current Issue: Mistral Tool Calling

**Problem:** Mistral Small 3.1 24B (`mistral-small-3-1-24b-instruct-2503-fp8`) outputs raw JSON tool calls as text instead of structured `tool_calls` objects via vLLM, even with `--tool-call-parser=mistral` and `--chat-template=tool_chat_template_mistral3.jinja`.

**Evidence:**
- Direct vLLM test with `tool_choice: "auto"` → `tool_calls: []`, content contains raw JSON `[{"name": "knowledge_search", ...}]`
- Direct vLLM test with `tool_choice: "required"` → `tool_calls: 1`, structured JSON ✅
- LlamaStack sends `tool_choice: "auto"` (we can't control this)
- The `mistral` parser does not intercept tool calls under `tool_choice: "auto"` for this model variant
- Tried all 3 available templates: `mistral.jinja`, `mistral_parallel.jinja`, `mistral3.jinja` — none work with `auto`

**Root cause:** vLLM's `mistral` tool-call parser has a compatibility issue with this specific Mistral model variant when `tool_choice` is `"auto"`. Additionally, Mistral's tokenizer requires tool call IDs exactly 9 digits long, which conflicts with vLLM's generated IDs.

**Options to resolve:**

1. **Swap model** — Replace with `mistralai/Mistral-Nemo-Instruct-2407` (12B). Older model but better vLLM compatibility. Requires new modelcar image + LLMInferenceService. Moderate effort.

2. **Swap model** — Try `Mistral-Small-3.2-24B-Instruct-2506` (newer variant). May have better vLLM support. Requires checking if Red Hat has a modelcar image for it.

3. **Mark Mistral as non-RAG** — Restore per-model `rag_enabled` logic in notebook-api's model discovery (currently hardcoded to `True` for all models). Mistral works for plain chat, just not with file_search tool calling. Low effort, but reduces demo impact.

4. **Implement fallback path** — For models where the Responses API fails, manually retrieve context via vector store search and inject into chat completions prompt. Goes against the "thin wrapper" principle. Not recommended.

5. **Wait for vLLM/LlamaStack fixes** — Newer vLLM versions or LlamaStack v0.4+ may resolve the parser issue. Zero effort on our side.

**Current state:** Mistral template is set to `mistral3.jinja` (commit `27e9007`). Mistral works for plain chat but outputs raw tool call JSON for RAG queries. Users see `[{"name": "knowledge_search", ...}]` as the response when using Mistral on notebooks with documents.

---

## Feature Enhancements Backlog

### Completed (6)

| Feature | Implementation |
|---|---|
| Source upload + RAG chat | Files API + vector_stores + Responses API with file_search |
| Notebook + document management | vector_stores CRUD, files CRUD, delete buttons in UI |
| Chat history persistence | GET /v1/responses (stored in sql_store.db), loaded on notebook select |
| Multi-model RAG | Data-driven ConfigMap, per-model remote::vllm providers, all models in dropdown |
| Simulated streaming | Non-streaming Responses API → word-level SSE chunks with 20ms delays |
| Source citations in chat | file_citation annotations from Responses API → deduplicated badges in UI |

### Can Build — UI/API Wrapper Work Only (7)

| Feature | Implementation | Effort | Notes |
|---|---|---|---|
| Multi-turn conversation | Pass `previous_response_id` in Responses API | 2h | LlamaStack manages context natively |
| Skip file_search on empty notebooks | If `file_counts.completed == 0`, omit tools param | 30m | Prevents Llama/Mistral tool call leaks on empty notebooks |
| Study guide / FAQ generation | Responses API + prompt template, render as markdown | 3h | Same pattern as chat, different prompt |
| Flashcards / Quiz | Responses API + JSON output prompt, card flip UI | 4h | LLM generates, UI renders |
| Mind map | Responses API + JSON graph prompt, D3 render | 5h | LLM generates, UI renders |
| Data table extraction | Responses API + JSON table prompt, HTML table UI | 4h | LLM generates, UI renders |
| Scoped source selection | `file_search.filters` param in Responses API | 3h | Untested — needs validation. Would allow chatting with subset of documents |

### Blocked by LlamaStack v0.3.5 Bugs (2)

| Feature | Bug | Impact |
|---|---|---|
| Response grounding (answer only from docs) | `instructions` field causes `file_search` to be skipped entirely | Models answer from own knowledge instead of documents. Zero effort to fix once LlamaStack resolves the bug — just add the `instructions` field back. |
| Native streaming | `stream: true` doesn't inject file_search context into model prompt | Currently using non-streaming + simulated streaming workaround. Zero effort to switch once fixed. |

### Out of Scope — Needs Capabilities Beyond LlamaStack (4)

| Feature | Requires |
|---|---|
| Audio overview (podcast) | TTS model |
| Video overview | TTS + image gen + video composition |
| Slide deck generation | Image generation model |
| Infographic generation | Image generation model |

---

## Known LlamaStack v0.3.5 Bugs

1. **`instructions` + `file_search` conflict** — Providing `instructions` field in Responses API causes `file_search` to be skipped entirely. The model answers from its own knowledge. Workaround: removed `instructions` field.

2. **Streaming + `file_search`** — Streaming mode (`stream: true`) performs file_search but does NOT inject retrieved context into the model prompt. The model generates hallucinated answers. Workaround: use non-streaming `responses_sync()` + simulated streaming.

3. **RAG prompt leakage** — LlamaStack injects internal citation instructions into the system prompt. Models sometimes output these as text (e.g., "Cite sources immediately at the end of sentences..."). We do NOT strip this in the wrapper — it's a LlamaStack behavior. Will be resolved when the `instructions` bug is fixed.

---

## Key Files

| File | Purpose |
|---|---|
| `charts/llama-stack-instance/templates/configmap.yaml` | Data-driven LlamaStack config — loops over models list |
| `charts/llama-stack-instance/values.yaml` | Models list (Qwen, Llama, Mistral) |
| `charts/models/values.yaml` | vLLM model deployments — extraArgs with tool parsers + chat templates |
| `charts/notebook-api/app/main.py` | FastAPI proxy — chat, notebooks, documents, history endpoints |
| `charts/notebook-api/app/llamastack_client.py` | LlamaStack API client — responses_sync, list_responses, etc. |
| `charts/notebook-api/app/config.py` | Settings — llamastack_url, model_id defaults |
| `charts/notebook-ui/app/src/App.tsx` | React UI — notebooks, chat, model selector, citations |
| `charts/embed-model/` | Snowflake embedding Deployment+Service chart |
| `bootstrap/templates/applications/` | ArgoCD ApplicationSet definitions |

---

## Recent Git Commits (newest first)

```
7d1b761 fix: deduplicate models from LlamaStack by name
db2baf0 fix: discover models from LlamaStack instead of Kubernetes API
27e9007 fix: use mistral3 chat template for Mistral Small 3.1
b8b2c9e revert: remove RAG prompt leakage stripping
4bdf6ed feat: source citations in chat responses
a48cdd9 fix: add --chat-template for Llama and Mistral tool calling
54bded5 feat: all models RAG-enabled — data-driven LlamaStack ConfigMap
420637b feat: simulate streaming by sending response in word-level chunks
54b867e feat: conversation history — persists across sessions
e87b14a feat: delete individual documents from notebooks
2fa87da feat: add delete notebook button with confirmation dialog
d87c1e5 feat: add embed-model ArgoCD Application to bootstrap
f8ca88a fix: increase upload limit to 50MB
7986dd5 fix: use Deployment+Service for Snowflake embed
4fa24ca feat: switch to Snowflake Arctic Embed L v2.0 on GPU
```

---

## Key Principles (DO follow these)

1. **Thin wrapper** — The notebook-api is a passthrough to LlamaStack. Do NOT add custom logic for model behavior, response formatting, or retrieval. If something doesn't work, it's a LlamaStack or vLLM issue to track, not something to workaround in the wrapper.

2. **Assess before acting** — Always determine whether an issue is pre-existing and non-blocking before attempting a fix. Never modify working configuration while troubleshooting an unrelated issue.

3. **Validate live before committing** — Test fixes on the live cluster before committing to git. Check git status and revert uncommitted local changes before starting new work.

4. **GitOps is the single source of truth** — All fixes must be committed to the repo. Live-only changes are explicitly avoided.

5. **Don't over-fix** — Don't pursue changes unless they are clearly blocking something.

6. **No credentials in repo** — No cluster-specific identifiers, credentials, or real values committed to the public GitHub repo.

---

## Workflow Patterns

- **Local git workflow:** All file changes are made on the local clone at `/Users/aelnatsh/Lab/MaaS` using Desktop Commander, then committed and pushed to GitHub via `git add` → `git commit` → `git push origin main`.
- **ArgoCD sync:** `annotate refresh=hard` → `sleep 20` → status check → then `patch operation sync` if needed.
- **Cluster access:** `oc login` requires `--insecure-skip-tls-verify=true`.
- **Build and deploy notebook-api/ui:** `oc start-build notebook-api -n maas-rag` → wait for Complete → `oc rollout restart deployment/notebook-api -n maas-rag`.

---

## Immediate Next Actions

1. **Decide on Mistral** — Choose from the 5 options listed in "Current Issue: Mistral Tool Calling" above.
2. **Skip file_search on empty notebooks** (30min) — Prevents Llama and Mistral from leaking tool call text when no documents are uploaded.
3. **Multi-turn conversation** (2h) — Pass `previous_response_id` to maintain chat context across turns.
4. **HANDOVER.md** — Commit the updated handover doc to the repo.
