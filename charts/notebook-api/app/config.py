from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # MaaS gateway — used directly for inference (OpenAI-compatible)
    maas_base_url: str = "http://maas.apps.cluster.local"
    maas_token: str = ""

    # LlamaStack — kept for future use / memory bank ops (currently no-op)
    llamastack_url: str = "http://llamastack:8321"

    milvus_uri: str = "http://milvus:19530"
    embed_endpoint: str = ""
    embed_model: str = "sentence-transformers/nomic-ai/nomic-embed-text-v1.5"
    docling_url: str = ""
    max_chunk_size: int = 512
    chunk_overlap: int = 50
    top_k_results: int = 5
    score_threshold: float = 0.75
    embed_dim: int = 768

    model_config = {"env_prefix": ""}


settings = Settings()
