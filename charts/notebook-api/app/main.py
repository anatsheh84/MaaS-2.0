"""NotebookLM RAG API — thin proxy over LlamaStack native APIs.

All RAG state (vector stores, files, embeddings) is managed by LlamaStack
with persistent storage. This API adds:
  - OAuth user identity from X-Forwarded-User header
  - User-scoped notebook listing (vector store name prefix)
  - Model discovery from Kubernetes LLMInferenceService resources
  - SSE streaming wrapper for the Responses API
"""
import logging
from typing import Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import llamastack_client
from .config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="NotebookLM RAG API", version="3.0.0")


# ── Request / response models ──────────────────────────────────────────────

class NotebookCreate(BaseModel):
    name: str


class ChatRequest(BaseModel):
    query: str
    model: str = ""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_username(header: str | None) -> str:
    """Extract username from X-Forwarded-User, default to 'anonymous'."""
    return (header or "anonymous").strip()


def _notebook_prefix(username: str) -> str:
    """Vector store name prefix for user isolation."""
    return f"nb_{username}_"


# ── Health ──────────────────────────────────────────────────────────────────

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Model discovery ─────────────────────────────────────────────────────────

@app.get("/models")
async def list_models():
    """Discover deployed LLM models from Kubernetes LLMInferenceService resources."""
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_host = "https://kubernetes.default.svc"

    try:
        import os
        if os.path.exists(token_path):
            with open(token_path) as f:
                token = f.read().strip()
            async with httpx.AsyncClient(timeout=10.0, verify=ca_path) as client:
                resp = await client.get(
                    f"{k8s_host}/apis/serving.kserve.io/v1alpha1/namespaces/llm/llminferenceservices",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
            # The RAG model is the one configured in LlamaStack
            rag_model = settings.llamastack_model_id.split("/")[-1]
            models = [
                {
                    "value": item["metadata"]["name"],
                    "label": (
                        item["metadata"].get("annotations", {})
                        .get("openshift.io/display-name")
                        or item["metadata"]["name"].replace("-", " ").title()
                    ),
                    "rag_enabled": item["metadata"]["name"] == rag_model,
                }
                for item in items
                if item.get("status", {}).get("conditions", [{}])[0].get("status") != "False"
            ]
            logger.info("Discovered %d models from Kubernetes API", len(models))
            return {"models": models}
    except Exception as e:
        logger.warning("Kubernetes model discovery failed: %s", e)

    # Fallback — LlamaStack /v1/models
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.llamastack_url}/v1/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
        return {"models": [
            {"value": m["identifier"].split("/")[-1],
             "label": m["identifier"].split("/")[-1].replace("-", " ").title()}
            for m in data if m.get("model_type") == "llm"
        ]}
    except Exception as e:
        logger.warning("LlamaStack model fallback also failed: %s", e)
        return {"models": []}


# ── Notebooks (backed by LlamaStack vector stores) ─────────────────────────

@app.post("/notebooks", status_code=201)
async def create_notebook(
    body: NotebookCreate,
    x_forwarded_user: Optional[str] = Header(default=None),
):
    """Create a notebook = create a LlamaStack vector store with user-prefixed name."""
    username = _get_username(x_forwarded_user)
    vs_name = f"{_notebook_prefix(username)}{body.name.strip()}"

    vs = await llamastack_client.create_vector_store(vs_name)
    return {
        "notebook_id": vs["id"],
        "name": body.name.strip(),
        "vector_store_id": vs["id"],
    }


@app.get("/notebooks")
async def list_notebooks(
    x_forwarded_user: Optional[str] = Header(default=None),
):
    """List notebooks for the current user (filtered by VS name prefix)."""
    username = _get_username(x_forwarded_user)
    prefix = _notebook_prefix(username)

    all_vs = await llamastack_client.list_vector_stores()
    notebooks = []
    for vs in all_vs:
        name = vs.get("name", "")
        if name.startswith(prefix):
            notebooks.append({
                "notebook_id": vs["id"],
                "name": name[len(prefix):],
                "vector_store_id": vs["id"],
                "file_counts": vs.get("file_counts", {}),
                "status": vs.get("status", "unknown"),
                "created_at": vs.get("created_at"),
            })
    return {"notebooks": notebooks}


@app.get("/notebooks/{notebook_id}")
async def get_notebook(notebook_id: str):
    """Get a single notebook by its vector store ID."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")
    return {
        "notebook_id": vs["id"],
        "name": vs.get("name", ""),
        "vector_store_id": vs["id"],
        "file_counts": vs.get("file_counts", {}),
        "status": vs.get("status", "unknown"),
    }


@app.delete("/notebooks/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str):
    """Delete a notebook (= delete its vector store)."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")
    await llamastack_client.delete_vector_store(notebook_id)


# ── Documents ───────────────────────────────────────────────────────────────

@app.post("/notebooks/{notebook_id}/documents", status_code=202)
async def upload_document(
    notebook_id: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
):
    """Upload a document: file → LlamaStack /v1/files → attach to vector store.
    Attachment (chunking + embedding) runs in background."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")

    file_bytes = await file.read()
    filename = file.filename or "unnamed"

    # Upload file to LlamaStack
    ls_file = await llamastack_client.upload_file(filename, file_bytes)
    file_id = ls_file["id"]

    # Attach to vector store in a separate thread (chunking + embedding can take minutes)
    # FastAPI BackgroundTasks runs serially and blocks — use a thread instead
    import threading

    def _attach_thread():
        import httpx as sync_httpx
        try:
            logger.info("Background attach starting: file=%s vs=%s", file_id, notebook_id)
            with sync_httpx.Client(timeout=600.0) as client:
                resp = client.post(
                    f"{llamastack_client.settings.llamastack_url}/v1/vector_stores/{notebook_id}/files",
                    json={"file_id": file_id, "chunking_strategy": {
                        "type": "static",
                        "static": {"max_chunk_size_tokens": 400, "chunk_overlap_tokens": 50},
                    }},
                )
                resp.raise_for_status()
                result = resp.json()
            status = result.get("status", "unknown")
            logger.info("Background attach done: file=%s status=%s", file_id, status)
            if status == "failed":
                error_msg = result.get("last_error", {}).get("message", "Unknown")
                logger.error("Attach failed for %s: %s", file_id, error_msg)
        except Exception:
            logger.exception("Background attach failed for file %s", file_id)

    threading.Thread(target=_attach_thread, daemon=True).start()
    return {"file_id": file_id, "filename": filename, "status": "accepted"}


@app.get("/notebooks/{notebook_id}/documents")
async def list_documents(notebook_id: str):
    """List documents in a notebook (= files attached to the vector store)."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")

    vs_files = await llamastack_client.list_files_in_vector_store(notebook_id)

    # Resolve filenames — vector store file objects don't include filename,
    # so we fetch each file's metadata from /v1/files/{id}
    docs = []
    for f in vs_files:
        file_meta = await llamastack_client.get_file(f["id"])
        filename = file_meta.get("filename", f["id"]) if file_meta else f["id"]
        docs.append({
            "doc_id": f["id"],
            "filename": filename,
            "ingest_status": f.get("status", "unknown"),
        })
    return {"documents": docs}


@app.get("/notebooks/{notebook_id}/ingest-status")
async def get_ingest_status(notebook_id: str):
    """Get ingest status from the vector store file_counts."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")

    vs_files = await llamastack_client.list_files_in_vector_store(notebook_id)
    jobs = {}
    for f in vs_files:
        file_meta = await llamastack_client.get_file(f["id"])
        filename = file_meta.get("filename", f["id"]) if file_meta else f["id"]
        jobs[f["id"]] = {
            "doc_id": f["id"],
            "filename": filename,
            "status": f.get("status", "unknown"),
            "progress": 100 if f.get("status") == "completed" else 50,
        }
    return {"notebook_id": notebook_id, "jobs": jobs}


# ── Chat (via LlamaStack Responses API) ─────────────────────────────────────

@app.post("/notebooks/{notebook_id}/chat")
async def chat(
    notebook_id: str,
    body: ChatRequest,
    x_forwarded_user: Optional[str] = Header(default=None),
):
    """Stream a RAG chat response using LlamaStack's Responses API.

    LlamaStack handles retrieval from the vector store and inference
    through its enterprise-tier gateway token (no rate limits).
    The model parameter maps to a LlamaStack model identifier.
    """
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")

    username = _get_username(x_forwarded_user)
    logger.info("Chat: user=%s notebook=%s model=%s", username, notebook_id, body.model)

    # LlamaStack only has one inference provider (maas-vllm-inference-1 → Qwen).
    # Always use the configured default model for the Responses API.
    # The model selector in the UI is informational — multi-model support requires
    # registering additional providers in the LlamaStack ConfigMap.
    model_id = None  # will use settings.llamastack_model_id default

    async def stream_gen():
        """Use non-streaming Responses API (streaming has a LlamaStack v0.3.5 bug
        where file_search results aren't injected into the model context).
        Convert the complete response to SSE events for the UI.
        """
        import json as _json

        try:
            result = await llamastack_client.responses_sync(
                query=body.query,
                vector_store_ids=[notebook_id],
                model=model_id or None,
            )

            # Check for errors
            if result.get("error"):
                error_msg = result["error"].get("message", "Unknown error")
                compat = {"choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ Error: {error_msg}"}}]}
                yield f"data: {_json.dumps(compat)}\n\n"
            elif result.get("status") == "failed":
                error_msg = result.get("error", {}).get("message", "Response failed")
                compat = {"choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ Error: {error_msg}"}}]}
                yield f"data: {_json.dumps(compat)}\n\n"
            else:
                # Extract text from the response output
                for output in result.get("output", []):
                    if output.get("type") == "message":
                        for content in output.get("content", []):
                            if content.get("type") == "output_text":
                                text = content.get("text", "")
                                # Send as a single chunk (non-streaming)
                                if text:
                                    compat = {"choices": [{"index": 0, "delta": {"content": text}}]}
                                    yield f"data: {_json.dumps(compat)}\n\n"
        except Exception as e:
            logger.exception("Chat error for notebook %s", notebook_id)
            compat = {"choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ Error: {e}"}}]}
            yield f"data: {_json.dumps(compat)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_gen(), media_type="text/event-stream")
