from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LlamaStack — embeddings, vector store, file storage
    llamastack_url: str = "http://llamastack:8321"
    llamastack_embedding_model: str = "sentence-transformers/nomic-ai/nomic-embed-text-v1.5"

    # MaaS gateway — chat completions
    maas_base_url: str = "https://maas.apps.cluster.local"
    maas_token: str = ""

    # RAG tuning
    chunk_size: int = 512
    top_k_results: int = 5

    model_config = {"env_prefix": ""}


settings = Settings()
