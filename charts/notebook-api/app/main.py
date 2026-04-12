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
    """Discover LLM models registered in LlamaStack.

    Only models registered in the LlamaStack ConfigMap are returned — these are
    the models that can actually serve requests via the Responses API.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.llamastack_url}/v1/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
        models = []
        seen = set()
        for m in data:
            meta = m.get("custom_metadata", {})
            if meta.get("model_type", m.get("model_type")) != "llm":
                continue
            full_id = m.get("id", m.get("identifier", ""))
            display = full_id.split("/")[-1]
            if not full_id or full_id in seen:
                continue
            seen.add(full_id)
            models.append({
                "value": full_id,
                "label": (
                    meta.get("display_name")
                    or m.get("metadata", {}).get("display_name")
                    or display.replace("-", " ").title()
                ),
                "rag_enabled": True,
            })
        logger.info("Discovered %d models from LlamaStack", len(models))
        return {"models": models}
    except Exception as e:
        logger.warning("LlamaStack model discovery failed: %s", e)
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
                    json={"file_id": file_id, "chunking_strategy": {"type": "auto"}},
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


@app.delete("/notebooks/{notebook_id}/documents/{file_id}", status_code=204)
async def delete_document(notebook_id: str, file_id: str):
    """Remove a document from a notebook (deletes its embeddings from the vector store)."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")
    try:
        await llamastack_client.delete_file_from_vector_store(notebook_id, file_id)
    except Exception as e:
        raise HTTPException(500, f"Failed to delete document: {e}")


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

@app.get("/notebooks/{notebook_id}/history")
async def get_history(notebook_id: str):
    """Get conversation history for a notebook."""
    vs = await llamastack_client.get_vector_store(notebook_id)
    if not vs:
        raise HTTPException(404, "Notebook not found")
    history = await llamastack_client.list_responses(notebook_id)
    return {"history": history}


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

    # Model ID from the UI is already the full LlamaStack identifier
    model_id = body.model or None

    async def stream_gen():
        """Stream responses from LlamaStack's Responses API (v0.5.0+).

        LlamaStack 0.5.0 fixed the v0.3.5 bug where file_search results
        were not injected into model context during streaming. The streaming
        event format follows the OpenAI Responses API:
          - response.output_text.delta → text chunks
          - response.file_search_call.completed → search done
          - response.completed → final response with usage
        We convert these to the SSE format the frontend expects:
          data: {"choices": [{"index": 0, "delta": {"content": "..."}}]}
        """
        import json as _json
        import re as _re

        # Pattern to strip raw citation tokens like <|file-abc123|>
        _FILE_TOKEN_RE = _re.compile(r'<\|file-[a-f0-9]+\|>')

        try:
            citations = []
            citation_block_started = False
            async for chunk in llamastack_client.responses_stream(
                query=body.query,
                vector_store_ids=[notebook_id],
                model=model_id or None,
            ):
                try:
                    event = _json.loads(chunk)
                except (ValueError, TypeError):
                    continue

                event_type = event.get("type", "")

                # Text delta — the main streaming content
                if event_type == "response.output_text.delta":
                    delta_text = event.get("delta", "")
                    if delta_text:
                        # Detect start of citation block (model-generated)
                        if "Cite sources" in delta_text or "Cite source" in delta_text:
                            citation_block_started = True
                            continue
                        # Skip all text once citation block starts
                        if citation_block_started:
                            continue
                        # Strip any inline <|file-...|> tokens
                        delta_text = _FILE_TOKEN_RE.sub('', delta_text)
                        if delta_text:
                            compat = {"choices": [{"index": 0, "delta": {"content": delta_text}}]}
                            yield f"data: {_json.dumps(compat)}\n\n"

                # Completed response — extract citations from annotations
                elif event_type == "response.completed":
                    resp = event.get("response", {})
                    for output in resp.get("output", []):
                        if output.get("type") == "message":
                            for content in output.get("content", []):
                                for ann in content.get("annotations", []):
                                    if ann.get("type") == "file_citation":
                                        fn = ann.get("filename", "")
                                        if fn and fn not in [c["filename"] for c in citations]:
                                            citations.append({"file_id": ann.get("file_id", ""), "filename": fn})

                # Error in response
                elif event_type == "response.failed":
                    error_msg = event.get("response", {}).get("error", {}).get("message", "Response failed")
                    compat = {"choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ Error: {error_msg}"}}]}
                    yield f"data: {_json.dumps(compat)}\n\n"

            # Send citations after streaming completes
            if citations:
                yield f"data: {_json.dumps({'citations': citations})}\n\n"

        except Exception as e:
            logger.exception("Chat error for notebook %s", notebook_id)
            compat = {"choices": [{"index": 0, "delta": {"content": f"\n\n⚠️ Error: {e}"}}]}
            yield f"data: {_json.dumps(compat)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_gen(), media_type="text/event-stream")
