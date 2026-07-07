import os
from pathlib import Path


class Settings:
    opensearch_url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    opensearch_index = os.getenv("OPENSEARCH_INDEX", "legal-rag-content")
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
    minio_endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
    minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")
    minio_bucket = os.getenv("MINIO_BUCKET", "knowledge-root")
    samples_dir = Path(os.getenv("SAMPLES_DIR", "/workspace/samples"))
    eval_results_dir = Path(os.getenv("EVAL_RESULTS_DIR", "/workspace/eval-results"))
    embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
    llm_provider = os.getenv("LLM_PROVIDER", "ollama")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    planner_model = os.getenv("PLANNER_MODEL", "gemma4:e4b")
    answer_model = os.getenv("ANSWER_MODEL", "gemma4:e4b")
    judge_model = os.getenv("JUDGE_MODEL", "none")
    llm_timeout_sec = int(os.getenv("LLM_TIMEOUT_SEC", "90"))
    llm_max_context_chars = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "4000"))


settings = Settings()
