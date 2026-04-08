import io
import logging
import time
import uuid
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# In-memory ingest status tracker
ingest_status: dict[str, dict[str, Any]] = {}


def _extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract plain text from PDF, DOCX, or text files."""
    fname = filename.lower()

    if fname.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(p for p in pages if p.strip())
            if text.strip():
                logger.info("PDF: %d pages, %d chars from %s", len(reader.pages), len(text), filename)
                return text
            logger.warning("PDF yielded no text (scanned/image-only?): %s", filename)
        except Exception as e:
            logger.warning("PDF extraction failed for %s: %s", filename, e)

    if fname.endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n\n".join(paragraphs)
            if text.strip():
                logger.info("DOCX: %d paragraphs, %d chars from %s", len(paragraphs), len(text), filename)
                return text
            logger.warning("DOCX yielded no text: %s", filename)
        except Exception as e:
            logger.warning("DOCX extraction failed for %s: %s", filename, e)

    try:
        return file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return file_bytes.decode("latin-1", errors="replace")


async def ingest_document(
    notebook_id: str,
    doc_id: str,
    filename: str,
    file_bytes: bytes,
    vector_store_id: str,
) -> None:
    """Extract text → upload to LlamaStack files → attach to vector store (auto chunk+embed)."""
    status_key = f"{notebook_id}/{doc_id}"
    ingest_status[status_key] = {
        "doc_id": doc_id,
        "filename": filename,
        "status": "extracting",
        "progress": 10,
        "started_at": time.time(),
    }

    try:
        text = _extract_text(file_bytes, filename)
        if not text.strip():
            ingest_status[status_key].update({"status": "failed", "error": "No text extracted"})
            return

        ingest_status[status_key].update({"status": "uploading", "progress": 30})

        # Upload step — 60s is plenty for file transfer
        async with httpx.AsyncClient(timeout=60.0) as client:
            txt_filename = filename.rsplit(".", 1)[0] + ".txt"
            upload_resp = await client.post(
                f"{settings.llamastack_url}/v1/files",
                files={"file": (txt_filename, text.encode("utf-8"), "text/plain")},
                data={"purpose": "assistants"},
            )
            upload_resp.raise_for_status()
            file_id = upload_resp.json()["id"]
            logger.info("Uploaded %s → file_id=%s", filename, file_id)

        ingest_status[status_key].update({"status": "embedding", "progress": 60})

        # Embed step — separate client with long timeout.
        # Large files (100KB+) require LlamaStack to chunk and embed hundreds of
        # vectors via sentence-transformers which can take several minutes.
        async with httpx.AsyncClient(timeout=600.0) as client:
            attach_resp = await client.post(
                f"{settings.llamastack_url}/v1/vector_stores/{vector_store_id}/files",
                json={"file_id": file_id, "chunking_strategy": {"type": "auto"}},
            )
            attach_resp.raise_for_status()
            result = attach_resp.json()

            if result.get("status") == "failed":
                error_msg = result.get("last_error", {}).get("message", "Unknown error")
                logger.error("LlamaStack attach failed: %s", error_msg)
                ingest_status[status_key].update({"status": "failed", "error": error_msg})
                return

        ingest_status[status_key].update({"status": "completed", "progress": 100})
        logger.info("Ingested doc %s into vector store %s", doc_id, vector_store_id)

    except Exception:
        logger.exception("Ingest failed for doc %s", doc_id)
        ingest_status[status_key].update({"status": "failed", "error": "Ingest pipeline error"})
