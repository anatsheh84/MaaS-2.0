import logging
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import ingest, llamastack_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="NotebookLM RAG API", version="2.0.0")

# In-memory notebook store: {notebook_id: {name, vector_store_id, documents}}
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
    vector_store_id = None
    try:
        vector_store_id = await llamastack_client.create_vector_store(notebook_id)
    except Exception as e:
        logger.warning("Vector store creation deferred: %s", e)
    notebooks[notebook_id] = {
        "name": body.name,
        "vector_store_id": vector_store_id,
        "documents": [],
    }
    return {"notebook_id": notebook_id, "name": body.name}


@app.get("/notebooks/{notebook_id}")
async def get_notebook(notebook_id: str):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    doc_statuses = {k: v for k, v in ingest.ingest_status.items() if k.startswith(f"{notebook_id}/")}
    all_done = all(s["status"] in ("completed", "failed") for s in doc_statuses.values())
    return {
        "notebook_id": notebook_id,
        "name": nb["name"],
        "vector_store_id": nb["vector_store_id"],
        "doc_count": len(nb["documents"]),
        "ingest_status": "idle" if all_done else "processing",
    }


@app.delete("/notebooks/{notebook_id}", status_code=204)
async def delete_notebook(notebook_id: str):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    if nb.get("vector_store_id"):
        await llamastack_client.delete_vector_store(nb["vector_store_id"])
    del notebooks[notebook_id]
    for k in [k for k in ingest.ingest_status if k.startswith(f"{notebook_id}/")]:
        del ingest.ingest_status[k]


@app.post("/notebooks/{notebook_id}/documents", status_code=202)
async def upload_document(notebook_id: str, file: UploadFile, background_tasks: BackgroundTasks):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    if not nb.get("vector_store_id"):
        try:
            nb["vector_store_id"] = await llamastack_client.create_vector_store(notebook_id)
        except Exception as e:
            raise HTTPException(503, f"LlamaStack unavailable: {e}")

    doc_id = uuid.uuid4().hex[:12]
    file_bytes = await file.read()
    filename = file.filename or "unnamed"
    nb["documents"].append({"doc_id": doc_id, "filename": filename})
    background_tasks.add_task(
        ingest.ingest_document,
        notebook_id, doc_id, filename, file_bytes, nb["vector_store_id"],
    )
    return {"doc_id": doc_id, "filename": filename, "status": "accepted"}


@app.get("/notebooks/{notebook_id}/documents")
async def list_documents(notebook_id: str):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    docs = []
    for doc in nb["documents"]:
        status_key = f"{notebook_id}/{doc['doc_id']}"
        status_entry = ingest.ingest_status.get(status_key, {})
        docs.append({**doc, "ingest_status": status_entry.get("status", "unknown")})
    return {"documents": docs}


@app.post("/notebooks/{notebook_id}/chat")
async def chat(notebook_id: str, body: ChatRequest):
    nb = notebooks.get(notebook_id)
    if not nb:
        raise HTTPException(404, "Notebook not found")
    vector_store_id = nb.get("vector_store_id", "")

    async def stream_gen():
        async for chunk in llamastack_client.chat_stream(vector_store_id, body.query, body.model):
            yield f"data: {chunk}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream_gen(), media_type="text/event-stream")


@app.get("/notebooks/{notebook_id}/ingest-status")
async def get_ingest_status(notebook_id: str):
    if notebook_id not in notebooks:
        raise HTTPException(404, "Notebook not found")
    statuses = {
        k.split("/", 1)[1]: v
        for k, v in ingest.ingest_status.items()
        if k.startswith(f"{notebook_id}/")
    }
    return {"notebook_id": notebook_id, "jobs": statuses}
