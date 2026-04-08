import logging
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from . import ingest, llamastack_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="NotebookLM RAG API", version="0.1.0")

# In-memory notebook store — sufficient for demo
notebooks: dict[str, dict] = {}


class NotebookCreate(BaseModel):
    name: str


class ChatRequest(BaseModel):
    query: str
    model: str = "qwen3-4b-instruct"


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/notebooks", status_code=201)
async def create_notebook(body: NotebookCreate):
    notebook_id = uuid.uuid4().hex[:12]
    # Register memory bank lazily — don't fail notebook creation if LlamaStack unavailable
    try:
        await llamastack_client.register_memory_bank(notebook_id)
        bank_registered = True
    except Exception as e:
        logger.warning("LlamaStack memory bank registration deferred (will retry on upload): %s", e)
        bank_registered = False
    notebooks[notebook_id] = {
        "name": body.name,
        "documents": [],
        "bank_registered": bank_registered,
    }
    return {"notebook_id": notebook_id, "name": body.name}


@app.get("/notebooks/{notebook_id}")
async def get_notebook(notebook_id: str):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    doc_statuses = {
        k: v for k, v in ingest.ingest_status.items()
        if k.startswith(f"{notebook_id}/")
    }
    all_done = all(s["status"] in ("completed", "failed") for s in doc_statuses.values())
    return {
        "notebook_id": notebook_id,
        "name": nb["name"],
        "doc_count": len(nb["documents"]),
        "ingest_status": "idle" if all_done else "processing",
    }


@app.delete("/notebooks/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str):
    if notebook_id not in notebooks:
        raise HTTPException(status_code=404, detail="Notebook not found")
    ingest.drop_collection(notebook_id)
    try:
        await llamastack_client.unregister_memory_bank(notebook_id)
    except Exception as e:
        logger.warning("LlamaStack unregister failed (non-critical): %s", e)
    del notebooks[notebook_id]
    for k in [k for k in ingest.ingest_status if k.startswith(f"{notebook_id}/")]:
        del ingest.ingest_status[k]

@app.post("/notebooks/{notebook_id}/documents", status_code=202)
async def upload_document(notebook_id: str, file: UploadFile, background_tasks: BackgroundTasks):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")

    # Retry memory bank registration if it failed at notebook creation time
    if not nb.get("bank_registered"):
        try:
            await llamastack_client.register_memory_bank(notebook_id)
            nb["bank_registered"] = True
        except Exception as e:
            logger.warning("LlamaStack still unavailable — ingest will proceed, chat will fail: %s", e)

    doc_id = uuid.uuid4().hex[:12]
    file_bytes = await file.read()
    filename = file.filename or "unnamed"
    nb["documents"].append({"doc_id": doc_id, "filename": filename})
    background_tasks.add_task(ingest.ingest_document, notebook_id, doc_id, filename, file_bytes)
    return {"doc_id": doc_id, "filename": filename, "status": "accepted"}


@app.get("/notebooks/{notebook_id}/documents")
async def list_documents(notebook_id: str):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(status_code=404, detail="Notebook not found")
    docs = []
    for doc in nb["documents"]:
        status_key = f"{notebook_id}/{doc['doc_id']}"
        status_entry = ingest.ingest_status.get(status_key, {})
        docs.append({**doc, "ingest_status": status_entry.get("status", "unknown")})
    return {"documents": docs}


@app.post("/notebooks/{notebook_id}/chat")
async def chat(notebook_id: str, body: ChatRequest):
    if notebook_id not in notebooks:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return EventSourceResponse(
        llamastack_client.chat_stream(notebook_id, body.query, body.model)
    )


@app.get("/notebooks/{notebook_id}/ingest-status")
async def get_ingest_status(notebook_id: str):
    if notebook_id not in notebooks:
        raise HTTPException(status_code=404, detail="Notebook not found")
    statuses = {
        k.split("/", 1)[1]: v
        for k, v in ingest.ingest_status.items()
        if k.startswith(f"{notebook_id}/")
    }
    return {"notebook_id": notebook_id, "jobs": statuses}
