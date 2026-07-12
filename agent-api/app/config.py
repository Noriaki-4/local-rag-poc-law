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
    lawqa_eval_path = os.getenv("LAWQA_EVAL_PATH")
    lawqa_eval_url = os.getenv("LAWQA_EVAL_URL")
    seed_lawqa_egov = os.getenv("SEED_LAWQA_EGOV", "false").lower() in {"1", "true", "yes", "on"}
    lawqa_egov_law_ids = os.getenv("LAWQA_EGOV_LAW_IDS", "")
    egov_api_base_url = os.getenv("EGOV_API_BASE_URL", "https://laws.e-gov.go.jp/api/1")
    agent_use_bm25 = os.getenv("AGENT_USE_BM25", "true").lower() in {"1", "true", "yes", "on"}
    agent_use_vector = os.getenv("AGENT_USE_VECTOR", "true").lower() in {"1", "true", "yes", "on"}
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "ollama")
    embedding_model = os.getenv("EMBEDDING_MODEL", "bge-m3")
    embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
    embedding_timeout_sec = int(os.getenv("EMBEDDING_TIMEOUT_SEC", "120"))
    embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
    embedding_max_chars = int(os.getenv("EMBEDDING_MAX_CHARS", "1000"))
    agent_max_queries = max(1, min(int(os.getenv("AGENT_MAX_QUERIES", "4")), 8))
    agent_max_retry_rounds = max(0, min(int(os.getenv("AGENT_MAX_RETRY_ROUNDS", "1")), 2))
    agent_max_total_tool_calls = max(1, min(int(os.getenv("AGENT_MAX_TOTAL_TOOL_CALLS", "8")), 16))
    agent_max_graph_hop = max(1, min(int(os.getenv("AGENT_MAX_GRAPH_HOP", "1")), 3))
    agent_max_graph_paths = max(1, min(int(os.getenv("AGENT_MAX_GRAPH_PATHS", "10")), 50))
    agent_max_wall_time_sec = max(10, min(int(os.getenv("AGENT_MAX_WALL_TIME_SEC", "110")), 300))
    agent_use_llm_planner = os.getenv("AGENT_USE_LLM_PLANNER", "true").lower() in {"1", "true", "yes", "on"}
    agent_candidate_top_k = max(5, min(int(os.getenv("AGENT_CANDIDATE_TOP_K", "20")), 100))
    agent_rerank_top_k = max(1, min(int(os.getenv("AGENT_RERANK_TOP_K", "10")), 50))
    agent_rrf_k = max(1, int(os.getenv("AGENT_RRF_K", "60")))
    agent_max_llm_calls = max(1, min(int(os.getenv("AGENT_MAX_LLM_CALLS", "3")), 3))
    llm_provider = os.getenv("LLM_PROVIDER", "ollama")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    anthropic_version = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    anthropic_max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "4096"))
    answer_model = os.getenv("ANSWER_MODEL") or ("claude-sonnet-5" if llm_provider == "anthropic" else "gemma4:e4b")
    planner_model = os.getenv("PLANNER_MODEL") or answer_model
    planner_max_tokens = int(os.getenv("PLANNER_MAX_TOKENS", "1024"))
    planner_timeout_sec = int(os.getenv("PLANNER_TIMEOUT_SEC", "30"))
    evaluator_model = os.getenv("EVALUATOR_MODEL") or planner_model
    evaluator_max_tokens = int(os.getenv("EVALUATOR_MAX_TOKENS", "1024"))
    evaluator_timeout_sec = int(os.getenv("EVALUATOR_TIMEOUT_SEC", "20"))
    judge_model = os.getenv("JUDGE_MODEL", "none")
    llm_timeout_sec = int(os.getenv("LLM_TIMEOUT_SEC", "90"))
    llm_max_context_chars = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "4000"))


settings = Settings()
