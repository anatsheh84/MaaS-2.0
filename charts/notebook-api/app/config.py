from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LlamaStack — all RAG operations (files, vector stores, responses)
    llamastack_url: str = "http://llamastack:8321"
    llamastack_embedding_model: str = "sentence-transformers/granite-embed"
    llamastack_model_id: str = "maas-vllm-inference-1/qwen3-4b-instruct"

    # MaaS gateway — model discovery only (chat goes through LlamaStack)
    maas_base_url: str = "https://maas.apps.cluster.local"
    maas_token: str = ""

    # RAG tuning
    top_k_results: int = 10

    model_config = {"env_prefix": ""}


settings = Settings()
