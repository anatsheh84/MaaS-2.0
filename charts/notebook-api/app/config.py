from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llamastack_url: str = "http://llamastack:8321"
    milvus_uri: str = "http://milvus:19530"
    embed_endpoint: str = "http://nomic-embed-predictor.maas-rag/v1"
    embed_model: str = "nomic-embed-text-v1.5"
    docling_url: str = ""
    max_chunk_size: int = 512
    chunk_overlap: int = 50
    top_k_results: int = 5
    score_threshold: float = 0.75
    embed_dim: int = 768

    model_config = {"env_prefix": ""}


settings = Settings()
