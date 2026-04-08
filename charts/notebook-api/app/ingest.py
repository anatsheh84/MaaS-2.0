import asyncio
import hashlib
import logging
import os
import time
from typing import Any

import httpx
from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility
from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)

# In-memory ingest status tracker — sufficient for demo
ingest_status: dict[str, dict[str, Any]] = {}

# Load embedding model once at startup — all-MiniLM-L6-v2 is 384-dim, fast, CPU-friendly.
# Override EMBED_MODEL_NAME to use a different sentence-transformers model.
_EMBED_MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "all-MiniLM-L6-v2")
logger.info("Loading embedding model: %s", _EMBED_MODEL_NAME)
_embed_model = SentenceTransformer(_EMBED_MODEL_NAME)
COLLECTION_DIM = _embed_model.get_sentence_embedding_dimension()
logger.info("Embedding model loaded — dimension: %d", COLLECTION_DIM)


def _collection_name(notebook_id: str) -> str:
    return f"notebook_{notebook_id}"


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Recursive character splitting without LangChain."""
    separators = ["\n\n", "\n", ". ", " ", ""]
    chunks: list[str] = []

    def _split(text: str, seps: list[str]) -> list[str]:
        if len(text) <= chunk_size:
            return [text] if text.strip() else []
        if not seps:
            # No separators left — hard-split
            result = []
            for i in range(0, len(text), chunk_size - overlap):
                piece = text[i : i + chunk_size]
                if piece.strip():
                    result.append(piece)
            return result

        sep = seps[0]
        remaining_seps = seps[1:]

        if sep == "":
            return _split(text, remaining_seps)

        parts = text.split(sep)
        current = ""
        result = []

        for part in parts:
            candidate = f"{current}{sep}{part}" if current else part
            if len(candidate) <= chunk_size:
                current = candidate
            else:
                if current:
                    result.append(current)
                if len(part) > chunk_size:
                    result.extend(_split(part, remaining_seps))
                else:
                    current = part
                    continue
                current = ""

        if current.strip():
            result.append(current)
        return result

    raw_chunks = _split(text, separators)

    # Apply overlap by including tail of previous chunk
    for i, chunk in enumerate(raw_chunks):
        if i > 0 and overlap > 0:
            prev_tail = raw_chunks[i - 1][-overlap:]
            chunk = prev_tail + chunk
        chunks.append(chunk.strip())

    return [c for c in chunks if c]


async def _extract_text_docling(file_bytes: bytes, filename: str) -> str:
    """Extract text via Docling REST API."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{settings.docling_url}/convert",
            files={"file": (filename, file_bytes)},
        )
        resp.raise_for_status()
        return resp.json().get("text", "")


def _extract_text_basic(file_bytes: bytes, filename: str) -> str:
    """Basic text extraction fallback — handles plain text and common formats."""
    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1", errors="replace")


async def _get_embeddings(texts: list[str]) -> list[list[float]]:
    """Embed texts locally using sentence-transformers — no external service needed."""
    loop = asyncio.get_event_loop()
    embeddings = await loop.run_in_executor(
        None, lambda: _embed_model.encode(texts, show_progress_bar=False).tolist()
    )
    return embeddings


def _ensure_collection(notebook_id: str) -> Collection:
    """Create Milvus collection if it doesn't exist."""
    col_name = _collection_name(notebook_id)
    connections.connect(alias="default", uri=settings.milvus_uri)

    if utility.has_collection(col_name):
        return Collection(col_name)

    fields = [
        FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
        FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
        FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=COLLECTION_DIM),
    ]
    schema = CollectionSchema(fields=fields, description=f"RAG store for notebook {notebook_id}")
    collection = Collection(name=col_name, schema=schema)
    collection.create_index(
        field_name="embedding",
        index_params={"metric_type": "COSINE", "index_type": "IVF_FLAT", "params": {"nlist": 128}},
    )
    return collection


def drop_collection(notebook_id: str) -> None:
    """Drop the Milvus collection for a notebook."""
    col_name = _collection_name(notebook_id)
    connections.connect(alias="default", uri=settings.milvus_uri)
    if utility.has_collection(col_name):
        utility.drop_collection(col_name)


async def ingest_document(notebook_id: str, doc_id: str, filename: str, file_bytes: bytes) -> None:
    """Full ingest pipeline: extract → chunk → embed → upsert into Milvus."""
    status_key = f"{notebook_id}/{doc_id}"
    ingest_status[status_key] = {
        "doc_id": doc_id,
        "filename": filename,
        "status": "extracting",
        "progress": 0,
        "started_at": time.time(),
    }

    try:
        # 1. Extract text
        if settings.docling_url:
            text = await _extract_text_docling(file_bytes, filename)
        else:
            text = _extract_text_basic(file_bytes, filename)

        if not text.strip():
            ingest_status[status_key].update({"status": "failed", "error": "No text extracted"})
            return

        # 2. Chunk
        ingest_status[status_key]["status"] = "chunking"
        chunks = _chunk_text(text, settings.max_chunk_size, settings.chunk_overlap)
        total_chunks = len(chunks)
        ingest_status[status_key]["total_chunks"] = total_chunks

        # 3. Embed in batches
        ingest_status[status_key]["status"] = "embedding"
        batch_size = 32
        all_embeddings: list[list[float]] = []
        for i in range(0, total_chunks, batch_size):
            batch = chunks[i : i + batch_size]
            embeddings = await _get_embeddings(batch)
            all_embeddings.extend(embeddings)
            ingest_status[status_key]["progress"] = int(len(all_embeddings) / total_chunks * 80)

        # 4. Upsert into Milvus
        ingest_status[status_key]["status"] = "upserting"
        collection = _ensure_collection(notebook_id)

        ids = [
            hashlib.sha256(f"{doc_id}:{i}".encode()).hexdigest()[:16] for i in range(total_chunks)
        ]
        collection.insert([ids, chunks, [doc_id] * total_chunks, all_embeddings])
        collection.flush()
        collection.load()

        ingest_status[status_key].update(
            {"status": "completed", "progress": 100, "chunks_stored": total_chunks}
        )
        logger.info("Ingested %d chunks for doc %s in notebook %s", total_chunks, doc_id, notebook_id)

    except Exception:
        logger.exception("Ingest failed for doc %s in notebook %s", doc_id, notebook_id)
        ingest_status[status_key].update({"status": "failed", "error": "Ingest pipeline error"})
