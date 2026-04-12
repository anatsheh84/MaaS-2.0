"""LlamaStack client — thin wrapper around native OpenAI-compatible APIs.

All RAG operations (files, vector stores, responses) go through LlamaStack.
LlamaStack handles chunking, embedding, retrieval, and inference internally
using its enterprise-tier gateway token (no rate limits).
"""
import logging
from collections.abc import AsyncIterator

import httpx

from .config import settings

logger = logging.getLogger(__name__)

_BASE = settings.llamastack_url


# ── Vector Stores (= Notebooks) ────────────────────────────────────────────

async def create_vector_store(name: str) -> dict:
    """Create a vector store. Returns the full VS object."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        body = {
            "name": name,
            "embedding_model": settings.llamastack_embedding_model,
        }
        if settings.llamastack_vector_provider:
            body["provider_id"] = settings.llamastack_vector_provider
        resp = await client.post(
            f"{_BASE}/v1/vector_stores",
            json=body,
        )
        resp.raise_for_status()
        vs = resp.json()
        logger.info("Created vector store %s (name=%s)", vs["id"], name)
        return vs


async def list_vector_stores() -> list[dict]:
    """List all vector stores."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{_BASE}/v1/vector_stores")
        resp.raise_for_status()
        return resp.json().get("data", [])


async def get_vector_store(vs_id: str) -> dict | None:
    """Get a single vector store by ID. Returns None if not found."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_BASE}/v1/vector_stores/{vs_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Failed to get vector store %s: %s", vs_id, e)
        return None


async def delete_vector_store(vs_id: str) -> None:
    """Delete a vector store."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(f"{_BASE}/v1/vector_stores/{vs_id}")
        logger.info("Deleted vector store %s", vs_id)
    except Exception as e:
        logger.warning("Failed to delete vector store %s: %s", vs_id, e)


async def delete_file_from_vector_store(vs_id: str, file_id: str) -> None:
    """Remove a file from a vector store (deletes its embeddings)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(f"{_BASE}/v1/vector_stores/{vs_id}/files/{file_id}")
        resp.raise_for_status()
    logger.info("Deleted file %s from vector store %s", file_id, vs_id)


# ── Files ───────────────────────────────────────────────────────────────────

async def upload_file(filename: str, content: bytes) -> dict:
    """Upload a file to LlamaStack. Returns the file object."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_BASE}/v1/files",
            files={"file": (filename, content, "application/octet-stream")},
            data={"purpose": "assistants"},
        )
        resp.raise_for_status()
        f = resp.json()
        logger.info("Uploaded file %s → id=%s", filename, f["id"])
        return f


async def list_files_in_vector_store(vs_id: str) -> list[dict]:
    """List files attached to a vector store."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_BASE}/v1/vector_stores/{vs_id}/files")
            resp.raise_for_status()
            return resp.json().get("data", [])
    except Exception as e:
        logger.warning("Failed to list files for vs %s: %s", vs_id, e)
        return []


async def get_file(file_id: str) -> dict | None:
    """Get file metadata by ID. Returns the file object with filename."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{_BASE}/v1/files/{file_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Failed to get file %s: %s", file_id, e)
        return None


async def attach_file_to_vector_store(vs_id: str, file_id: str) -> dict:
    """Attach a file to a vector store (triggers chunking + embedding).
    Long timeout for large files."""
    chunking = {"type": "auto"}
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            f"{_BASE}/v1/vector_stores/{vs_id}/files",
            json={"file_id": file_id, "chunking_strategy": chunking},
        )
        resp.raise_for_status()
        result = resp.json()
        logger.info("Attached file %s to vs %s (status=%s)",
                     file_id, vs_id, result.get("status"))
        return result


# ── Responses API (RAG chat) ───────────────────────────────────────────────

async def responses_stream(
    query: str,
    vector_store_ids: list[str],
    model: str | None = None,
) -> AsyncIterator[str]:
    """Stream a response from the LlamaStack Responses API with file_search.

    Uses LlamaStack's built-in file_search tool which handles retrieval
    from vector stores and feeds context to the model automatically.
    Yields SSE-compatible JSON chunks (OpenAI streaming format).
    """
    model_id = model or settings.llamastack_model_id

    payload = {
        "model": model_id,
        "input": query,
        "stream": True,
        "instructions": (
            "You are a helpful AI assistant. When documents are available, use them "
            "to answer questions accurately. When no relevant documents are found "
            "or the question is casual/conversational, respond naturally "
            "without mentioning search results, tools, or internal processes. "
            "Never expose internal tool mechanics to the user. "
            "Do not add citation references, source lists, or file IDs at the end "
            "of your response — citations are handled automatically by the system."
        ),
        "tools": [
            {
                "type": "file_search",
                "vector_store_ids": vector_store_ids,
                "max_num_results": settings.top_k_results,
            }
        ],
    }

    logger.info("Responses API: model=%s vs=%s", model_id, vector_store_ids)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{_BASE}/v1/responses",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as stream:
            if stream.status_code >= 400:
                body = await stream.aread()
                logger.error("Responses API error %d: %s", stream.status_code, body)
                yield f'{{"error": "LlamaStack error {stream.status_code}"}}'
                return
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload_str = line[6:].strip()
                if payload_str == "[DONE]":
                    return
                yield payload_str


async def responses_sync(
    query: str,
    vector_store_ids: list[str],
    model: str | None = None,
) -> dict:
    """Non-streaming response for simple queries or fallback."""
    model_id = model or settings.llamastack_model_id

    payload = {
        "model": model_id,
        "input": query,
        "instructions": (
            "You are a helpful AI assistant. When documents are available, use them "
            "to answer questions accurately. When no relevant documents "
            "are found or the question is casual/conversational, respond naturally "
            "without mentioning search results, tools, or internal processes. "
            "Never expose internal tool mechanics to the user. "
            "Do not add citation references, source lists, or file IDs at the end "
            "of your response — citations are handled automatically by the system."
        ),
        "tools": [
            {
                "type": "file_search",
                "vector_store_ids": vector_store_ids,
                "max_num_results": settings.top_k_results,
            }
        ],
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{_BASE}/v1/responses",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()



async def list_responses(vector_store_id: str) -> list[dict]:
    """Fetch conversation history from LlamaStack, filtered by vector store ID.
    Returns a list of {query, answer, created_at, model} dicts, newest first."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{_BASE}/v1/responses")
        resp.raise_for_status()
        data = resp.json()

    results = []
    for r in data.get("data", []):
        # Filter by vector_store_id
        vs_ids = []
        for t in r.get("tools", []):
            vs_ids.extend(t.get("vector_store_ids", []))
        if vector_store_id not in vs_ids:
            continue

        # Extract query from input
        query = ""
        for inp in r.get("input", []):
            for c in inp.get("content", []):
                if c.get("type") == "input_text":
                    query = c["text"]

        # Extract answer from output
        answer = ""
        for o in r.get("output", []):
            if o.get("type") == "message":
                for c in o.get("content", []):
                    if c.get("type") == "output_text":
                        answer = c["text"]

        if query or answer:
            results.append({
                "id": r.get("id", ""),
                "query": query,
                "answer": answer,
                "created_at": r.get("created_at", 0),
                "model": r.get("model", ""),
            })

    return results
