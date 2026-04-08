import logging
from collections.abc import AsyncIterator

import httpx
from pymilvus import Collection, connections

from .config import settings
from .ingest import _collection_name, _get_embeddings

logger = logging.getLogger(__name__)

# Model name cache: short name → full LlamaStack identifier
# e.g. "qwen3-4b-instruct" → "maas-vllm-inference-1/qwen3-4b-instruct"
_model_map: dict[str, str] = {}


async def _build_model_map() -> None:
    """Fetch available LLM models from LlamaStack and build short-name lookup."""
    global _model_map
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.llamastack_url}/v1/models")
            resp.raise_for_status()
            models = resp.json().get("data", [])
            _model_map = {
                m["identifier"].split("/")[-1]: m["identifier"]
                for m in models
                if m.get("model_type") == "llm"
            }
            logger.info("LlamaStack model map built: %s", _model_map)
    except Exception as e:
        logger.warning("Could not build model map from LlamaStack: %s", e)


def _resolve_model(model: str) -> str:
    """Resolve short model name to full LlamaStack identifier."""
    if model in _model_map.values():
        return model  # already fully qualified
    resolved = _model_map.get(model, model)
    if resolved == model and _model_map:
        logger.warning("Model '%s' not in LlamaStack map %s — using as-is", model, list(_model_map))
    return resolved


async def register_memory_bank(notebook_id: str) -> None:
    """No-op — memory banks removed in LlamaStack 0.3.5+.
    RAG is handled by Milvus retrieval + context injection in chat_stream.
    We use this call to (re)build the model map while we're at it."""
    if not _model_map:
        await _build_model_map()
    logger.info("register_memory_bank: no-op for notebook %s", notebook_id)


async def unregister_memory_bank(notebook_id: str) -> None:
    """No-op — memory banks removed in LlamaStack 0.3.5+."""
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
                    chunks.append(hit.entity.get("text", ""))
        logger.info("Retrieved %d context chunks for notebook %s", len(chunks), notebook_id)
        return chunks

    except Exception:
        logger.exception("Milvus retrieval failed for notebook %s", notebook_id)
        return []


async def chat_stream(notebook_id: str, query: str, model: str) -> AsyncIterator[str]:
    """Retrieve context from Milvus, inject into prompt, stream via LlamaStack /v1/chat/completions."""

    # Ensure model map is populated
    if not _model_map:
        await _build_model_map()

    # 1. Resolve model name to full LlamaStack identifier
    resolved_model = _resolve_model(model)
    logger.info("Chat: notebook=%s model=%s -> %s", notebook_id, model, resolved_model)

    # 2. Retrieve relevant context from Milvus
    context_chunks = await _retrieve_context(notebook_id, query)

    # 3. Build system prompt with injected context
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
            "No document context is available for this query — "
            "let the user know they should upload and ingest documents first."
        )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": query},
    ]

    # 4. Stream via /v1/chat/completions
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{settings.llamastack_url}/v1/chat/completions",
            json={
                "model": resolved_model,
                "messages": messages,
                "stream": True,
                "temperature": 0.3,
                "max_tokens": 2048,
            },
        ) as stream:
            stream.raise_for_status()
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break
                yield f"data: {payload}\n\n"
