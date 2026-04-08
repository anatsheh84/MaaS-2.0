import logging
from collections.abc import AsyncIterator

import httpx
from pymilvus import Collection, connections

from .config import settings
from .ingest import _collection_name, _get_embeddings

logger = logging.getLogger(__name__)


async def register_memory_bank(notebook_id: str) -> None:
    """No-op — inference is via MaaS gateway directly, no LlamaStack memory banks needed."""
    logger.info("register_memory_bank: no-op for notebook %s", notebook_id)


async def unregister_memory_bank(notebook_id: str) -> None:
    """No-op."""
    logger.info("unregister_memory_bank: no-op for notebook %s", notebook_id)


async def _retrieve_context(notebook_id: str, query: str) -> list[str]:
    """Retrieve relevant chunks from Milvus for the given query."""
    try:
        query_embedding = await _get_embeddings([query])
        col_name = _collection_name(notebook_id)
        connections.connect(alias="default", uri=settings.milvus_uri)

        from pymilvus import utility
        if not utility.has_collection(col_name):
            logger.warning("Collection %s not found — no context available", col_name)
            return []

        collection = Collection(col_name)
        collection.load()

        results = collection.search(
            data=query_embedding,
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=settings.top_k_results,
            output_fields=["text"],
        )

        chunks = []
        for hits in results:
            for hit in hits:
                if hit.distance >= settings.score_threshold:
                    chunks.append(hit.fields.get("text", ""))
        logger.info("Retrieved %d context chunks for notebook %s", len(chunks), notebook_id)
        return chunks

    except Exception:
        logger.exception("Milvus retrieval failed for notebook %s", notebook_id)
        return []


async def chat_stream(notebook_id: str, query: str, model: str) -> AsyncIterator[str]:
    """Retrieve context from Milvus, inject into prompt, stream via MaaS gateway."""

    # 1. Retrieve relevant context from Milvus
    context_chunks = await _retrieve_context(notebook_id, query)

    # 2. Build system prompt with injected context
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
            "No document context is available — "
            "ask the user to upload and ingest documents first."
        )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": query},
    ]

    # 3. Stream via MaaS gateway OpenAI-compatible endpoint
    url = f"{settings.maas_base_url}/llm/{model}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.maas_token}",
    }
    logger.info("Chat: notebook=%s model=%s url=%s", notebook_id, model, url)

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json={
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        ) as stream:
            if stream.status_code >= 400:
                error_body = await stream.aread()
                logger.error("MaaS gateway error %d: %s", stream.status_code, error_body)
                yield f"{{\"error\": \"Gateway error {stream.status_code}\"}}"
                return

            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    return
                yield payload
