import logging
import uuid

import httpx

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


@app.get("/models")
async def list_models():
    """Discover deployed LLM models via the Kubernetes API (in-cluster SA token).
    Queries LLMInferenceService resources in the llm namespace — the authoritative
    source of what models are deployed and accessible via the MaaS gateway.
    Falls back to LlamaStack /v1/models if Kubernetes API is unavailable.
    """
    # In-cluster: token and CA cert are auto-mounted by OpenShift
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    k8s_host = "https://kubernetes.default.svc"

    try:
        import os
        if os.path.exists(token_path):
            with open(token_path) as f:
                token = f.read().strip()
            async with httpx.AsyncClient(timeout=10.0, verify=ca_path) as client:
                resp = await client.get(
                    f"{k8s_host}/apis/serving.kserve.io/v1alpha1/namespaces/llm/llminferenceservices",
                    headers={"Authorization": f"Bearer {token}"},
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])
            models = [
                {
                    "value": item["metadata"]["name"],
                    "label": (
                        item["metadata"].get("annotations", {})
                        .get("openshift.io/display-name")
                        or item["metadata"]["name"].replace("-", " ").title()
                    ),
                }
                for item in items
                if item.get("status", {}).get("conditions", [{}])[0].get("status") != "False"
            ]
            logger.info("Discovered %d models from Kubernetes API", len(models))
            return {"models": models}
    except Exception as e:
        logger.warning("Kubernetes API model discovery failed, falling back to LlamaStack: %s", e)

    # Fallback — LlamaStack /v1/models
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{llamastack_client.settings.llamastack_url}/v1/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
        return {"models": [
            {"value": m["identifier"].split("/")[-1],
             "label": m["identifier"].split("/")[-1].replace("-", " ").title()}
            for m in data if m.get("model_type") == "llm"
        ]}
    except Exception as e:
        logger.warning("LlamaStack fallback also failed: %s", e)
        return {"models": []}


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


# ── Tier token resolution ───────────────────────────────────────────────────
# In-cluster paths auto-mounted by OpenShift
_K8S_HOST = "https://kubernetes.default.svc"
_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
_TIER_NAMESPACES = [
    "maas-default-gateway-tier-free",
    "maas-default-gateway-tier-premium",
]
_MAAS_AUDIENCE = "maas-default-gateway-sa"
_TOKEN_TTL = 3600  # 1 hour


async def _get_user_maas_token(username: str) -> dict | None:
    """
    Find the per-user tier SA (created by maas-api) and issue a short-lived
    token for the MaaS gateway. Returns {token, tier, namespace} or None.

    SA naming convention: {username}-{8char-hex-hash}
    Label: app.kubernetes.io/component=token-issuer
    """
    import os, ssl
    if not os.path.exists(_TOKEN_PATH):
        logger.warning("Not running in-cluster — cannot resolve user token")
        return None

    with open(_TOKEN_PATH) as f:
        sa_token = f.read().strip()

    ctx = ssl.create_default_context(cafile=_CA_PATH)

    # Search tier namespaces from highest to lowest (premium before free)
    # so users get their highest entitled tier
    for ns in reversed(_TIER_NAMESPACES):
        try:
            url = (
                f"{_K8S_HOST}/api/v1/namespaces/{ns}/serviceaccounts"
                f"?labelSelector=app.kubernetes.io%2Fcomponent%3Dtoken-issuer"
                f"%2Cmaas.opendatahub.io%2Finstance%3Dmaas-default-gateway"
            )
            req = httpx.Request("GET", url, headers={"Authorization": f"Bearer {sa_token}"})
            async with httpx.AsyncClient(verify=_CA_PATH, timeout=10.0) as client:
                resp = await client.send(req)
                resp.raise_for_status()
                items = resp.json().get("items", [])

            # Match SA whose name starts with {username}-
            user_sa = next(
                (sa for sa in items if sa["metadata"]["name"].startswith(f"{username}-")),
                None,
            )
            if not user_sa:
                continue

            sa_name = user_sa["metadata"]["name"]
            tier = user_sa["metadata"]["labels"].get("maas.opendatahub.io/tier", "unknown")

            # Issue a short-lived token for this SA
            token_url = (
                f"{_K8S_HOST}/api/v1/namespaces/{ns}"
                f"/serviceaccounts/{sa_name}/token"
            )
            body = {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenRequest",
                "spec": {
                    "audiences": [_MAAS_AUDIENCE],
                    "expirationSeconds": _TOKEN_TTL,
                },
            }
            async with httpx.AsyncClient(verify=_CA_PATH, timeout=10.0) as client:
                tresp = await client.post(
                    token_url,
                    json=body,
                    headers={"Authorization": f"Bearer {sa_token}"},
                )
                tresp.raise_for_status()
                token = tresp.json()["status"]["token"]
                expires = tresp.json()["status"]["expirationTimestamp"]

            logger.info("Issued MaaS token for %s (SA: %s, tier: %s)", username, sa_name, tier)
            return {"token": token, "tier": tier, "namespace": ns, "expires": expires}

        except Exception as e:
            logger.debug("Tier namespace %s lookup failed for %s: %s", ns, username, e)
            continue

    logger.warning("No MaaS SA found for user %s in any tier namespace", username)
    return None


@app.get("/user-token/{username}")
async def get_user_token(username: str):
    """
    Return a short-lived MaaS gateway token for the given user.
    The token is scoped to the user's highest tier SA (created by maas-api).
    Returns 404 if the user has no MaaS access granted.
    """
    result = await _get_user_maas_token(username)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No MaaS access found for user '{username}'. "
                   f"Ensure the user has been onboarded via the MaaS portal.",
        )
    return result
