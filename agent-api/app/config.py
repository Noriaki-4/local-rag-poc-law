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
    seed_external_guidance = os.getenv("SEED_EXTERNAL_GUIDANCE", "false").lower() in {"1", "true", "yes", "on"}
    external_guidance_manifest = Path(
        os.getenv("EXTERNAL_GUIDANCE_MANIFEST", "/workspace/datasets/lawqa_jp/external-guidance/manifest.json")
    )
    external_guidance_chunk_chars = max(400, min(int(os.getenv("EXTERNAL_GUIDANCE_CHUNK_CHARS", "900")), 3000))
    agent_use_bm25 = os.getenv("AGENT_USE_BM25", "true").lower() in {"1", "true", "yes", "on"}
    agent_use_vector = os.getenv("AGENT_USE_VECTOR", "true").lower() in {"1", "true", "yes", "on"}
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "ollama")
    embedding_model = os.getenv("EMBEDDING_MODEL", "bge-m3")
    embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
    embedding_timeout_sec = int(os.getenv("EMBEDDING_TIMEOUT_SEC", "120"))
    embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
    embedding_max_chars = int(os.getenv("EMBEDDING_MAX_CHARS", "1000"))
    agent_max_queries = max(2, min(int(os.getenv("AGENT_MAX_QUERIES", "5")), 8))
    agent_max_retry_rounds = max(0, min(int(os.getenv("AGENT_MAX_RETRY_ROUNDS", "1")), 2))
    agent_max_total_tool_calls = max(1, min(int(os.getenv("AGENT_MAX_TOTAL_TOOL_CALLS", "8")), 16))
    agent_max_graph_hop = max(1, min(int(os.getenv("AGENT_MAX_GRAPH_HOP", "1")), 3))
    agent_max_graph_paths = max(1, min(int(os.getenv("AGENT_MAX_GRAPH_PATHS", "10")), 50))
    agent_max_wall_time_sec = max(10, min(int(os.getenv("AGENT_MAX_WALL_TIME_SEC", "110")), 300))
    agent_answer_reserve_sec = max(
        1,
        min(
            int(os.getenv("AGENT_ANSWER_RESERVE_SEC", "60")),
            max(1, agent_max_wall_time_sec - 1),
        ),
    )
    agent_issue_coverage_selection = os.getenv(
        "AGENT_ISSUE_COVERAGE_SELECTION", "false"
    ).lower() in {"1", "true", "yes", "on"}
    agent_issue_coverage_shadow = os.getenv(
        "AGENT_ISSUE_COVERAGE_SHADOW", "true"
    ).lower() in {"1", "true", "yes", "on"}
    agent_use_llm_planner = os.getenv("AGENT_USE_LLM_PLANNER", "true").lower() in {"1", "true", "yes", "on"}
    agent_candidate_top_k = max(5, min(int(os.getenv("AGENT_CANDIDATE_TOP_K", "20")), 100))
    agent_guidance_candidate_top_k = max(1, min(int(os.getenv("AGENT_GUIDANCE_CANDIDATE_TOP_K", "10")), 50))
    agent_rerank_top_k = max(1, min(int(os.getenv("AGENT_RERANK_TOP_K", "16")), 50))
    agent_rrf_k = max(1, int(os.getenv("AGENT_RRF_K", "60")))
    # shadow期間はplanner + 旧Evaluator + 新replan + answer で最大4 logical callsになる
    # (layered_legal_evidence_retrieval_plan.md §11.4)。既定は従来どおり3。
    agent_max_llm_calls = max(1, min(int(os.getenv("AGENT_MAX_LLM_CALLS", "3")), 4))
    rerank_provider = os.getenv("RERANK_PROVIDER", "none").lower()
    rerank_base_url = os.getenv("RERANK_BASE_URL", "http://localhost:8100")
    rerank_model = os.getenv("RERANK_MODEL", "hotchpotch/japanese-reranker-base-v2")
    rerank_candidate_top_k = max(2, min(int(os.getenv("RERANK_CANDIDATE_TOP_K", "30")), 100))
    rerank_timeout_sec = max(1, min(int(os.getenv("RERANK_TIMEOUT_SEC", "30")), 120))
    rerank_max_chars = max(256, min(int(os.getenv("RERANK_MAX_CHARS", "3000")), 12000))
    llm_provider = os.getenv("LLM_PROVIDER", "ollama")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    anthropic_version = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    # 応答にthinkingブロックが含まれ、その分もmax_tokensを消費する。思考だけで枠を使い切ると
    # 本文(text)が返らずJSONパースに失敗するため、本文が載る余裕を見て大きめに取る。
    anthropic_max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16384"))
    # 上限到達時に再試行で広げる際の天井。
    anthropic_max_tokens_ceiling = int(os.getenv("ANTHROPIC_MAX_TOKENS_CEILING", "32768"))
    answer_model = os.getenv("ANSWER_MODEL") or ("claude-sonnet-5" if llm_provider == "anthropic" else "gemma4:e4b")
    planner_model = os.getenv("PLANNER_MODEL") or answer_model
    planner_max_tokens = int(os.getenv("PLANNER_MAX_TOKENS", "1024"))
    planner_timeout_sec = int(os.getenv("PLANNER_TIMEOUT_SEC", "30"))
    evaluator_model = os.getenv("EVALUATOR_MODEL") or planner_model
    evaluator_max_tokens = int(os.getenv("EVALUATOR_MAX_TOKENS", "1024"))
    evaluator_timeout_sec = int(os.getenv("EVALUATOR_TIMEOUT_SEC", "20"))
    judge_model = os.getenv("JUDGE_MODEL", "none")
    llm_timeout_sec = int(os.getenv("LLM_TIMEOUT_SEC", "90"))
    llm_max_context_chars = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "12000"))

    # ------------------------------------------------------------------------------
    # 法令レイヤー別探索 vNext (docs/requirements/docs/layered_legal_evidence_retrieval_plan.md)
    # 既定はshadowのみ。active切替はPhase 6の評価後に行う(§19)。
    # 上限値は §11.1 の初期値案であり、Phase 0の実測後に確定する。
    # ------------------------------------------------------------------------------
    agent_layered_legal_retrieval = os.getenv(
        "AGENT_LAYERED_LEGAL_RETRIEVAL", "false"
    ).lower() in {"1", "true", "yes", "on"}
    agent_layered_legal_retrieval_shadow = os.getenv(
        "AGENT_LAYERED_LEGAL_RETRIEVAL_SHADOW", "false"
    ).lower() in {"1", "true", "yes", "on"}
    layered_max_primary_issues = max(1, min(int(os.getenv("LAYERED_MAX_PRIMARY_ISSUES", "8")), 16))
    layered_active_issue_batch_size = max(
        1, min(int(os.getenv("LAYERED_ACTIVE_ISSUE_BATCH_SIZE", "4")), 8)
    )
    layered_max_requirements_total = max(
        1, min(int(os.getenv("LAYERED_MAX_REQUIREMENTS_TOTAL", "24")), 96)
    )
    layered_max_legal_hops = max(1, min(int(os.getenv("LAYERED_MAX_LEGAL_HOPS", "3")), 5))
    layered_max_expansion_rounds = max(
        0, min(int(os.getenv("LAYERED_MAX_EXPANSION_ROUNDS", "3")), 5)
    )
    layered_max_articles_per_requirement = max(
        1, min(int(os.getenv("LAYERED_MAX_ARTICLES_PER_REQUIREMENT", "8")), 32)
    )
    layered_max_accepted_articles_per_requirement = max(
        1, min(int(os.getenv("LAYERED_MAX_ACCEPTED_ARTICLES_PER_REQUIREMENT", "2")), 8)
    )
    layered_max_child_relations_per_article = max(
        1, min(int(os.getenv("LAYERED_MAX_CHILD_RELATIONS_PER_ARTICLE", "6")), 20)
    )
    layered_max_article_candidates_total = max(
        1, min(int(os.getenv("LAYERED_MAX_ARTICLE_CANDIDATES_TOTAL", "64")), 256)
    )
    # Phase 0のCross-Encoder実測でper-call/全体のペア上限を確定するまでの暫定値。
    layered_max_rerank_pairs_per_call = max(
        1, min(int(os.getenv("LAYERED_MAX_RERANK_PAIRS_PER_CALL", "16")), 128)
    )
    layered_max_rerank_pairs_total = max(
        1, min(int(os.getenv("LAYERED_MAX_RERANK_PAIRS_TOTAL", "64")), 512)
    )
    layered_max_rerank_calls_per_round = max(
        0, min(int(os.getenv("LAYERED_MAX_RERANK_CALLS_PER_ROUND", "2")), 8)
    )
    layered_max_rerank_calls_total = max(
        0, min(int(os.getenv("LAYERED_MAX_RERANK_CALLS_TOTAL", "8")), 32)
    )
    layered_max_search_batch_calls_per_round = max(
        1, min(int(os.getenv("LAYERED_MAX_SEARCH_BATCH_CALLS_PER_ROUND", "2")), 8)
    )
    layered_max_search_batch_calls_total = max(
        1, min(int(os.getenv("LAYERED_MAX_SEARCH_BATCH_CALLS_TOTAL", "8")), 32)
    )
    layered_max_graph_batch_calls_per_round = max(
        0, min(int(os.getenv("LAYERED_MAX_GRAPH_BATCH_CALLS_PER_ROUND", "2")), 8)
    )
    layered_max_graph_batch_calls_total = max(
        0, min(int(os.getenv("LAYERED_MAX_GRAPH_BATCH_CALLS_TOTAL", "8")), 32)
    )
    layered_max_embedding_batch_calls_per_round = max(
        0, min(int(os.getenv("LAYERED_MAX_EMBEDDING_BATCH_CALLS_PER_ROUND", "1")), 4)
    )
    layered_max_embedding_batch_calls_total = max(
        0, min(int(os.getenv("LAYERED_MAX_EMBEDDING_BATCH_CALLS_TOTAL", "4")), 16)
    )
    layered_max_chunks_per_article = max(
        1, min(int(os.getenv("LAYERED_MAX_CHUNKS_PER_ARTICLE", "3")), 8)
    )
    # 文字予算を再設定するまで16を維持する(§11.5)。
    layered_final_context_chunks = max(
        1, min(int(os.getenv("LAYERED_FINAL_CONTEXT_CHUNKS", "16")), 24)
    )
    layered_max_auxiliary_context_chunks = max(
        0, min(int(os.getenv("LAYERED_MAX_AUXILIARY_CONTEXT_CHUNKS", "2")), 8)
    )
    layered_max_guidance_per_issue = max(
        0, min(int(os.getenv("LAYERED_MAX_GUIDANCE_PER_ISSUE", "5")), 20)
    )
    layered_max_guide_derived_articles = max(
        0, min(int(os.getenv("LAYERED_MAX_GUIDE_DERIVED_ARTICLES", "6")), 20)
    )
    layered_max_replan_calls = max(0, min(int(os.getenv("LAYERED_MAX_REPLAN_CALLS", "1")), 2))
    layered_shadow_phase_budget_sec = max(
        1, min(int(os.getenv("LAYERED_SHADOW_PHASE_BUDGET_SEC", "20")), 240)
    )
    layered_shadow_remaining_fraction = min(
        1.0, max(0.0, float(os.getenv("LAYERED_SHADOW_REMAINING_FRACTION", "0.5")))
    )
    # 採用profile名。Phase 0の実測後に明示する(§11.2)。未設定なら実値から推定する。
    agent_time_profile_name = os.getenv("AGENT_TIME_PROFILE_NAME", "")


settings = Settings()
