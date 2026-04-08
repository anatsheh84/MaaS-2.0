import logging
from collections.abc import AsyncIterator

import httpx

from .config import settings

logger = logging.getLogger(__name__)


async def register_memory_bank(notebook_id: str) -> dict:
    """Register a Milvus-backed memory bank in LlamaStack."""
    bank_id = f"notebook_{notebook_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.llamastack_url}/v1/memory_banks",
            json={
                "memory_bank_id": bank_id,
                "params": {
                    "memory_bank_type": "vector",
                    "embedding_model": settings.embed_model,
                    "chunk_size_in_tokens": settings.max_chunk_size,
                    "overlap_size_in_tokens": settings.chunk_overlap,
                },
                "provider_id": "milvus",
                "provider_params": {
                    "collection_name": f"notebook_{notebook_id}",
                    "uri": settings.milvus_uri,
                },
            },
        )
        resp.raise_for_status()
        return resp.json()


async def unregister_memory_bank(notebook_id: str) -> None:
    """Unregister a memory bank from LlamaStack."""
    bank_id = f"notebook_{notebook_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{settings.llamastack_url}/v1/memory_banks/{bank_id}",
        )
        # Ignore 404 — bank may already be gone
        if resp.status_code != 404:
            resp.raise_for_status()


async def chat_stream(notebook_id: str, query: str, model: str) -> AsyncIterator[str]:
    """Create a LlamaStack agent turn with RAG tool and stream tokens as SSE data."""
    bank_id = f"notebook_{notebook_id}"

    create_payload = {
        "agent_config": {
            "model": model,
            "instructions": (
                "You are a helpful research assistant. Answer the user's question "
                "using ONLY the retrieved document context. Cite specific passages. "
                "If the context does not contain enough information, say so."
            ),
            "tools": [
                {
                    "type": "memory",
                    "memory_bank_configs": [
                        {
                            "bank_id": bank_id,
                            "type": "vector",
                        }
                    ],
                    "query_generator_config": {"type": "default", "sep": " "},
                    "max_tokens_in_context": 4096,
                    "max_chunks": settings.top_k_results,
                }
            ],
            "sampling_params": {
                "strategy": {"type": "top_p", "temperature": 0.3, "top_p": 0.9},
                "max_tokens": 2048,
            },
        }
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Create agent session
        agent_resp = await client.post(
            f"{settings.llamastack_url}/v1/agents",
            json=create_payload,
        )
        agent_resp.raise_for_status()
        agent_id = agent_resp.json()["agent_id"]

        session_resp = await client.post(
            f"{settings.llamastack_url}/v1/agents/session/create",
            json={"agent_id": agent_id, "session_name": f"chat-{notebook_id}"},
        )
        session_resp.raise_for_status()
        session_id = session_resp.json()["session_id"]

        # Stream the turn
        async with client.stream(
            "POST",
            f"{settings.llamastack_url}/v1/agents/turn/create",
            json={
                "agent_id": agent_id,
                "session_id": session_id,
                "messages": [{"role": "user", "content": query}],
                "stream": True,
            },
        ) as stream:
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    yield "data: [DONE]\n\n"
                    break
                yield f"data: {payload}\n\n"

        # Cleanup — fire and forget
        try:
            await client.post(
                f"{settings.llamastack_url}/v1/agents/delete",
                json={"agent_id": agent_id},
            )
        except Exception:
            logger.debug("Agent cleanup failed for %s — non-critical", agent_id)
