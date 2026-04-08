import logging
from collections.abc import AsyncIterator

import httpx

from .config import settings

logger = logging.getLogger(__name__)


async def create_vector_store(notebook_id: str) -> str:
    """Create a LlamaStack vector store for a notebook. Returns vector_store_id."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.llamastack_url}/v1/vector_stores",
            json={
                "name": f"notebook_{notebook_id}",
                "embedding_model": settings.llamastack_embedding_model,
            },
        )
        resp.raise_for_status()
        vs_id = resp.json()["id"]
        logger.info("Created vector store %s for notebook %s", vs_id, notebook_id)
        return vs_id


async def delete_vector_store(vector_store_id: str) -> None:
    """Delete a LlamaStack vector store."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.delete(f"{settings.llamastack_url}/v1/vector_stores/{vector_store_id}")
        logger.info("Deleted vector store %s", vector_store_id)
    except Exception as e:
        logger.warning("Failed to delete vector store %s: %s", vector_store_id, e)


async def _retrieve_context(vector_store_id: str, query: str) -> list[str]:
    """Search vector store for relevant chunks."""
    if not vector_store_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.llamastack_url}/v1/vector_stores/{vector_store_id}/search",
                json={"query": query, "max_num_results": settings.top_k_results},
            )
            resp.raise_for_status()
            data = resp.json()

        chunks = []
        for item in data.get("data", []):
            for block in item.get("content", []):
                if block.get("type") == "text":
                    chunks.append(block["text"])

        logger.info("Retrieved %d chunks from vector store %s", len(chunks), vector_store_id)
        return chunks
    except Exception:
        logger.exception("Retrieval failed for vector store %s", vector_store_id)
        return []


async def chat_stream(vector_store_id: str, query: str, model: str) -> AsyncIterator[str]:
    """Retrieve context from LlamaStack, stream chat via MaaS gateway."""

    context_chunks = await _retrieve_context(vector_store_id, query)

    if context_chunks:
        context_text = "\n\n---\n\n".join(context_chunks)
        system_content = (
            "You are a helpful research assistant. Answer the user's question "
            "using ONLY the document context provided below. Cite specific passages. "
            "If the context does not contain enough information, say so.\n\n"
            f"DOCUMENT CONTEXT:\n{context_text}"
        )
    else:
        system_content = (
            "You are a helpful research assistant. "
            "No document context is available yet. "
            "Answer from your general knowledge, or ask the user to upload documents first."
        )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": query},
    ]

    url = f"{settings.maas_base_url}/llm/{model}/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {settings.maas_token}"}
    logger.info("Chat: model=%s vs=%s", model, vector_store_id)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST", url, headers=headers,
            json={"model": model, "messages": messages, "stream": True,
                  "temperature": 0.3, "max_tokens": 2048},
        ) as stream:
            if stream.status_code >= 400:
                body = await stream.aread()
                logger.error("Gateway error %d: %s", stream.status_code, body)
                yield f"{{\"error\": \"Gateway error {stream.status_code}\"}}"
                return
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    return
                yield payload
