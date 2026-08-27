import os
from pathlib import Path


class Settings:
    opensearch_url = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
    opensearch_index = os.getenv("OPENSEARCH_INDEX", "legal-rag-content-ja-v2")
    opensearch_index_mapping = Path(
        os.getenv(
            "OPENSEARCH_INDEX_MAPPING",
            "metadata/opensearch_index_mapping.japanese.sample.json",
        )
    )
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
    legal_relation_classification_run_id = os.getenv(
        "LEGAL_RELATION_CLASSIFICATION_RUN_ID", ""
    ).strip()
    minio_endpoint = os.getenv("MINIO_ENDPOINT", "localhost:9000")
    minio_access_key = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key = os.getenv("MINIO_SECRET_KEY", "minioadmin")
    minio_bucket = os.getenv("MINIO_BUCKET", "knowledge-root")
    samples_dir = Path(os.getenv("SAMPLES_DIR", "/workspace/samples"))
    eval_results_dir = Path(os.getenv("EVAL_RESULTS_DIR", "/workspace/eval-results"))
    lawqa_eval_path = os.getenv("LAWQA_EVAL_PATH")
    lawqa_eval_url = os.getenv("LAWQA_EVAL_URL")
    seed_lawqa_egov = os.getenv("SEED_LAWQA_EGOV", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    lawqa_egov_law_ids = os.getenv("LAWQA_EGOV_LAW_IDS", "")
    egov_api_base_url = os.getenv("EGOV_API_BASE_URL", "https://laws.e-gov.go.jp/api/1")
    egov_law_corpus_manifest = Path(
        os.getenv(
            "EGOV_LAW_CORPUS_MANIFEST",
            "/workspace/datasets/lawqa_jp/egov_law_corpus/manifest.json",
        )
    )
    _seed_scenario_manifest_value = os.getenv("SEED_SCENARIO_MANIFEST", "").strip()
    seed_scenario_manifest = (
        Path(_seed_scenario_manifest_value)
        if _seed_scenario_manifest_value
        else None
    )
    seed_external_guidance = os.getenv("SEED_EXTERNAL_GUIDANCE", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    external_guidance_manifest = Path(
        os.getenv(
            "EXTERNAL_GUIDANCE_MANIFEST",
            "/workspace/datasets/lawqa_jp/external-guidance/manifest.json",
        )
    )
    external_guidance_chunk_chars = max(
        400, min(int(os.getenv("EXTERNAL_GUIDANCE_CHUNK_CHARS", "900")), 3000)
    )
    agent_use_bm25 = os.getenv("AGENT_USE_BM25", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    agent_use_vector = os.getenv("AGENT_USE_VECTOR", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    embedding_provider = os.getenv("EMBEDDING_PROVIDER", "ollama")
    embedding_model = os.getenv("EMBEDDING_MODEL", "bge-m3")
    embedding_dimension = int(os.getenv("EMBEDDING_DIMENSION", "1024"))
    embedding_timeout_sec = int(os.getenv("EMBEDDING_TIMEOUT_SEC", "120"))
    embedding_batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", "8"))
    embedding_max_chars = int(os.getenv("EMBEDDING_MAX_CHARS", "1000"))
    agent_max_queries = max(2, min(int(os.getenv("AGENT_MAX_QUERIES", "5")), 8))
    agent_max_retry_rounds = max(
        0, min(int(os.getenv("AGENT_MAX_RETRY_ROUNDS", "1")), 2)
    )
    agent_max_total_tool_calls = max(
        1, min(int(os.getenv("AGENT_MAX_TOTAL_TOOL_CALLS", "8")), 16)
    )
    agent_max_graph_hop = max(1, min(int(os.getenv("AGENT_MAX_GRAPH_HOP", "1")), 3))
    agent_max_graph_paths = max(
        1, min(int(os.getenv("AGENT_MAX_GRAPH_PATHS", "10")), 50)
    )
    agent_max_wall_time_sec = max(
        10,
        min(int(os.getenv("AGENT_MAX_WALL_TIME_SEC", "110")), 600),
    )
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
    agent_use_llm_planner = os.getenv("AGENT_USE_LLM_PLANNER", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    agent_candidate_top_k = max(
        5, min(int(os.getenv("AGENT_CANDIDATE_TOP_K", "20")), 100)
    )
    agent_guidance_candidate_top_k = max(
        1, min(int(os.getenv("AGENT_GUIDANCE_CANDIDATE_TOP_K", "10")), 50)
    )
    agent_rerank_top_k = max(1, min(int(os.getenv("AGENT_RERANK_TOP_K", "16")), 50))
    agent_rrf_k = max(1, int(os.getenv("AGENT_RRF_K", "60")))
    # shadow期間はplanner + 旧Evaluator + 新replan + answer で最大4 logical callsになる
    # (layered_legal_evidence_retrieval_plan.md §11.4)。既定は従来どおり3。
    agent_max_llm_calls = max(1, min(int(os.getenv("AGENT_MAX_LLM_CALLS", "3")), 4))
    rerank_provider = os.getenv("RERANK_PROVIDER", "none").lower()
    rerank_base_url = os.getenv("RERANK_BASE_URL", "http://localhost:8100")
    rerank_model = os.getenv("RERANK_MODEL", "hotchpotch/japanese-reranker-base-v2")
    rerank_candidate_top_k = max(
        2, min(int(os.getenv("RERANK_CANDIDATE_TOP_K", "30")), 100)
    )
    rerank_timeout_sec = max(1, min(int(os.getenv("RERANK_TIMEOUT_SEC", "30")), 120))
    rerank_max_chars = max(256, min(int(os.getenv("RERANK_MAX_CHARS", "3000")), 12000))
    llm_provider = os.getenv("LLM_PROVIDER", "ollama")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    anthropic_version = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_api_key = os.getenv("OPENAI_API_KEY")
    _openai_reasoning_effort = os.getenv(
        "OPENAI_REASONING_EFFORT", "low"
    ).strip().lower()
    openai_reasoning_effort = (
        _openai_reasoning_effort
        if _openai_reasoning_effort
        in {"none", "low", "medium", "high", "xhigh", "max"}
        else None
    )
    # 現行Profileが要求する出力上限を満たすアプリ側の天井。
    # OpenAI transportは各呼出しの要求値をこの範囲へ収める。
    openai_max_tokens_ceiling = max(
        1, min(int(os.getenv("OPENAI_MAX_TOKENS_CEILING", "16384")), 16384)
    )
    # 応答にthinkingブロックが含まれ、その分もmax_tokensを消費する。思考だけで枠を使い切ると
    # 本文(text)が返らずJSONパースに失敗するため、本文が載る余裕を見て大きめに取る。
    anthropic_max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "16384"))
    # 上限到達時に再試行で広げる際の天井。
    anthropic_max_tokens_ceiling = int(
        os.getenv("ANTHROPIC_MAX_TOKENS_CEILING", "32768")
    )
    _default_answer_model = (
        "claude-sonnet-5"
        if llm_provider == "anthropic"
        else "gpt-5.6-luna"
        if llm_provider == "openai"
        else "gemma4:e4b"
    )
    # LLM_MODELは全役割を一括で切り替える簡易設定。未指定時だけ役割別設定を使う。
    llm_model = os.getenv("LLM_MODEL", "").strip()
    answer_model = llm_model or os.getenv("ANSWER_MODEL") or _default_answer_model
    reviewer_model = llm_model or os.getenv("REVIEWER_MODEL") or answer_model
    reviewer_max_tokens = max(
        1024,
        min(
            int(os.getenv("REVIEWER_MAX_TOKENS", "8192")),
            anthropic_max_tokens_ceiling,
        ),
    )
    planner_model = llm_model or os.getenv("PLANNER_MODEL") or answer_model
    planner_max_tokens = int(os.getenv("PLANNER_MAX_TOKENS", "1024"))
    planner_timeout_sec = int(os.getenv("PLANNER_TIMEOUT_SEC", "30"))
    evaluator_model = llm_model or os.getenv("EVALUATOR_MODEL") or planner_model
    evaluator_max_tokens = int(os.getenv("EVALUATOR_MAX_TOKENS", "1024"))
    evaluator_timeout_sec = int(os.getenv("EVALUATOR_TIMEOUT_SEC", "20"))
    judge_model = os.getenv("JUDGE_MODEL", "none")
    llm_timeout_sec = int(os.getenv("LLM_TIMEOUT_SEC", "90"))
    llm_max_context_chars = int(os.getenv("LLM_MAX_CONTEXT_CHARS", "12000"))
    llm_finalization_material_max_items = max(
        1,
        min(int(os.getenv("LLM_FINALIZATION_MATERIAL_MAX_ITEMS", "24")), 60),
    )

    # ------------------------------------------------------------------------------
    # LLM主導の法令調査
    # activeでは旧planner/layered selectorを通さず、LLMが選んだ証拠を回答へ接続する。
    # ------------------------------------------------------------------------------
    agent_llm_directed_retrieval = os.getenv(
        "AGENT_LLM_DIRECTED_RETRIEVAL", "false"
    ).lower() in {"1", "true", "yes", "on"}
    agent_llm_directed_retrieval_shadow = os.getenv(
        "AGENT_LLM_DIRECTED_RETRIEVAL_SHADOW", "false"
    ).lower() in {"1", "true", "yes", "on"}
    # LLM_RESEARCH_MODELは従来設定との互換用。LLM_MODEL未指定時は役割別設定を優先する。
    llm_research_model = llm_model or os.getenv("LLM_RESEARCH_MODEL") or answer_model
    llm_research_stage_model = (
        llm_model or os.getenv("LLM_RESEARCH_STAGE_MODEL") or llm_research_model
    )
    llm_research_integration_model = (
        llm_model or os.getenv("LLM_RESEARCH_INTEGRATION_MODEL") or llm_research_model
    )
    # RelationAssertionの分類はseedとは分離したオフライン処理で行う。検索・回答用の
    # providerがAnthropicでも、分類だけローカルOllamaへ分離できるよう専用providerを持つ。
    relation_classifier_provider = os.getenv(
        "RELATION_CLASSIFIER_PROVIDER", "ollama"
    ).lower()
    relation_classifier_model = (
        os.getenv("RELATION_CLASSIFIER_MODEL")
        or (
            "gemma4:e4b"
            if relation_classifier_provider == "ollama"
            else llm_research_stage_model
        )
    )
    relation_classifier_reviewer_model = (
        os.getenv("RELATION_CLASSIFIER_REVIEWER_MODEL")
        or (
            relation_classifier_model
            if relation_classifier_provider == "ollama"
            else reviewer_model
        )
    )
    relation_classifier_context_tokens = max(
        4096,
        min(
            int(os.getenv("RELATION_CLASSIFIER_CONTEXT_TOKENS", "131072")),
            131072,
        ),
    )
    relation_classifier_batch_size = max(
        1, min(int(os.getenv("RELATION_CLASSIFIER_BATCH_SIZE", "1")), 8)
    )
    relation_classifier_batch_chars = max(
        4000,
        min(int(os.getenv("RELATION_CLASSIFIER_BATCH_CHARS", "30000")), 60000),
    )
    relation_classifier_max_tokens = max(
        1024,
        min(
            int(os.getenv("RELATION_CLASSIFIER_MAX_TOKENS", "4096")),
            anthropic_max_tokens_ceiling,
        ),
    )
    relation_classifier_timeout_sec = max(
        10, min(int(os.getenv("RELATION_CLASSIFIER_TIMEOUT_SEC", "120")), 600)
    )
    llm_research_max_tokens = max(
        256, min(int(os.getenv("LLM_RESEARCH_MAX_TOKENS", "4096")), 16384)
    )
    llm_research_stage_effort = os.getenv(
        "LLM_RESEARCH_STAGE_EFFORT",
        "low",
    ).lower()
    if llm_research_stage_effort not in {
        "low",
        "medium",
        "high",
        "max",
    }:
        llm_research_stage_effort = "low"
    llm_research_integration_max_tokens = max(
        512,
        min(
            int(
                os.getenv(
                    "LLM_RESEARCH_INTEGRATION_MAX_TOKENS",
                    "8192",
                )
            ),
            16384,
        ),
    )
    llm_research_integration_effort = os.getenv(
        "LLM_RESEARCH_INTEGRATION_EFFORT",
        "low",
    ).lower()
    if llm_research_integration_effort not in {
        "low",
        "medium",
        "high",
        "xhigh",
    }:
        llm_research_integration_effort = "low"
    llm_research_timeout_sec = max(
        1, min(int(os.getenv("LLM_RESEARCH_TIMEOUT_SEC", "90")), 180)
    )
    llm_research_max_turns = max(
        1, min(int(os.getenv("LLM_RESEARCH_MAX_TURNS", "3")), 8)
    )
    llm_research_max_actions_per_turn = max(
        1, min(int(os.getenv("LLM_RESEARCH_MAX_ACTIONS_PER_TURN", "4")), 8)
    )
    llm_research_max_tool_calls = max(
        1, min(int(os.getenv("LLM_RESEARCH_MAX_TOOL_CALLS", "18")), 36)
    )
    llm_research_search_top_k = max(
        1, min(int(os.getenv("LLM_RESEARCH_SEARCH_TOP_K", "8")), 30)
    )
    # 文書が未確定の横断検索はノイズを抑え、documentId確定後の法令内検索は
    # Article再現率を優先する。取得するのはArticleごとの代表chunkだけ。
    llm_research_document_search_top_k = max(
        1,
        min(
            int(os.getenv("LLM_RESEARCH_DOCUMENT_SEARCH_TOP_K", "30")),
            60,
        ),
    )
    llm_research_max_chunks_per_article = max(
        1,
        min(
            int(
                os.getenv(
                    "LLM_RESEARCH_MAX_CHUNKS_PER_ARTICLE",
                    "100",
                )
            ),
            200,
        ),
    )
    llm_research_shadow_budget_sec = max(
        1, min(int(os.getenv("LLM_RESEARCH_SHADOW_BUDGET_SEC", "180")), 300)
    )
    llm_research_active_budget_sec = max(
        1, min(int(os.getenv("LLM_RESEARCH_ACTIVE_BUDGET_SEC", "360")), 450)
    )
    llm_research_max_evidence_items = max(
        1, min(int(os.getenv("LLM_RESEARCH_MAX_EVIDENCE_ITEMS", "60")), 100)
    )
    llm_research_max_selected_evidence = max(
        1, min(int(os.getenv("LLM_RESEARCH_MAX_SELECTED_EVIDENCE", "16")), 24)
    )
    llm_research_evidence_chars = max(
        1000, min(int(os.getenv("LLM_RESEARCH_EVIDENCE_CHARS", "30000")), 50000)
    )

    # ------------------------------------------------------------------------------
    # 新しい汎用Agent Framework。明示的に有効化するまで現行/answerは切り替えない。
    # Reviewerは品質や質問内容から自動選択せず、この設定がtrueのときだけ呼ぶ。
    # ------------------------------------------------------------------------------
    agent_framework_active = os.getenv("AGENT_FRAMEWORK_ACTIVE", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    agent_framework_reviewer_enabled = os.getenv(
        "AGENT_FRAMEWORK_REVIEWER_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    _agent_framework_diagnostics_mode = os.getenv(
        "AGENT_FRAMEWORK_DIAGNOSTICS_MODE", "off"
    ).strip().lower()
    agent_framework_diagnostics_mode = (
        _agent_framework_diagnostics_mode
        if _agent_framework_diagnostics_mode in {"off", "status", "snapshot"}
        else "off"
    )
    _agent_framework_post_run_audit = os.getenv(
        "AGENT_FRAMEWORK_POST_RUN_AUDIT", "off"
    ).strip().lower()
    agent_framework_post_run_audit = (
        _agent_framework_post_run_audit
        if _agent_framework_post_run_audit in {"off", "on_demand"}
        else "off"
    )
    agent_framework_research_model = (
        llm_model
        or os.getenv("AGENT_FRAMEWORK_RESEARCH_MODEL")
        or llm_research_stage_model
    )
    agent_framework_integration_model = (
        llm_model
        or os.getenv("AGENT_FRAMEWORK_INTEGRATION_MODEL")
        or os.getenv("AGENT_FRAMEWORK_FINALIZE_MODEL")
        or llm_research_integration_model
    )
    # 旧設定名は既存環境との互換性のためだけに残す。
    agent_framework_finalize_model = agent_framework_integration_model
    agent_framework_reviewer_model = (
        llm_model or os.getenv("AGENT_FRAMEWORK_REVIEWER_MODEL") or reviewer_model
    )
    agent_framework_research_max_tokens = max(
        1024,
        min(
            int(os.getenv("AGENT_FRAMEWORK_RESEARCH_MAX_TOKENS", "4096")),
            anthropic_max_tokens_ceiling,
        ),
    )
    agent_framework_integration_max_tokens = max(
        1024,
        min(
            int(
                os.getenv("AGENT_FRAMEWORK_INTEGRATION_MAX_TOKENS")
                or os.getenv("AGENT_FRAMEWORK_FINALIZE_MAX_TOKENS", "8192")
            ),
            anthropic_max_tokens_ceiling,
        ),
    )
    agent_framework_post_run_audit_max_tokens = max(
        512,
        min(
            int(os.getenv("AGENT_FRAMEWORK_POST_RUN_AUDIT_MAX_TOKENS", "2048")),
            anthropic_max_tokens_ceiling,
        ),
    )
    agent_framework_finalize_max_tokens = agent_framework_integration_max_tokens
    agent_framework_reviewer_max_tokens = max(
        1024,
        min(
            int(os.getenv("AGENT_FRAMEWORK_REVIEWER_MAX_TOKENS", "8192")),
            anthropic_max_tokens_ceiling,
        ),
    )
    agent_framework_model_timeout_sec = max(
        10,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MODEL_TIMEOUT_SEC", "90")),
            180,
        ),
    )
    agent_framework_max_research_cycles = max(
        1,
        min(int(os.getenv("AGENT_FRAMEWORK_MAX_RESEARCH_CYCLES", "4")), 4),
    )
    agent_framework_max_tool_requests_per_step = max(
        1,
        min(
            int(
                os.getenv("AGENT_FRAMEWORK_MAX_TOOL_REQUESTS_PER_STEP")
                or os.getenv("AGENT_FRAMEWORK_MAX_TOOL_REQUESTS_PER_CYCLE", "5")
            ),
            16,
        ),
    )
    # 旧名は既存環境・参照との互換用。意味はCycle累計ではなく1 step上限。
    agent_framework_max_tool_requests_per_cycle = (
        agent_framework_max_tool_requests_per_step
    )
    agent_framework_max_fetched_resources_per_cycle = max(
        1,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MAX_FETCHED_RESOURCES_PER_CYCLE", "5")),
            32,
        ),
    )
    agent_framework_max_selected_frontier_per_step = max(
        1,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MAX_SELECTED_FRONTIER_PER_STEP", "3")),
            16,
        ),
    )
    agent_framework_max_graph_candidates_per_review_batch = max(
        1,
        min(
            int(
                os.getenv(
                    "AGENT_FRAMEWORK_MAX_GRAPH_CANDIDATES_PER_REVIEW_BATCH",
                    "20",
                )
            ),
            200,
        ),
    )
    agent_framework_max_parallel_tools = max(
        1,
        min(int(os.getenv("AGENT_FRAMEWORK_MAX_PARALLEL_TOOLS", "4")), 16),
    )
    agent_framework_max_retained_evidence = max(
        0,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MAX_RETAINED_EVIDENCE", "12")),
            60,
        ),
    )
    agent_framework_max_material_evidence_chars = max(
        1000,
        min(
            int(
                os.getenv(
                    "AGENT_FRAMEWORK_MAX_MATERIAL_EVIDENCE_CHARS",
                    "50000",
                )
            ),
            200000,
        ),
    )
    agent_framework_max_solver_input_chars = max(
        2000,
        min(
            int(
                os.getenv(
                    "AGENT_FRAMEWORK_MAX_SOLVER_INPUT_CHARS",
                    "240000",
                )
            ),
            1000000,
        ),
    )
    agent_framework_cycle_close_reserve_sec = max(
        5,
        min(
            int(os.getenv("AGENT_FRAMEWORK_CYCLE_CLOSE_RESERVE_SEC", "15")),
            179,
        ),
    )
    agent_framework_min_next_cycle_budget_sec = max(
        5,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MIN_NEXT_CYCLE_BUDGET_SEC", "25")),
            179,
        ),
    )
    agent_framework_finalization_reserve_sec = max(
        10,
        min(
            int(
                os.getenv("AGENT_FRAMEWORK_FINALIZATION_RESERVE_SEC")
                or os.getenv("AGENT_FRAMEWORK_NEXT_SOLVER_RESERVE_SEC", "35")
            ),
            179,
        ),
    )
    agent_framework_next_solver_reserve_sec = (
        agent_framework_finalization_reserve_sec
    )
    agent_framework_max_wall_time_sec = max(
        agent_framework_finalization_reserve_sec + 1,
        min(
            int(os.getenv("AGENT_FRAMEWORK_MAX_WALL_TIME_SEC", "300")),
            600,
        ),
    )
    agent_framework_reviewer_max_revisions = max(
        0,
        min(
            int(os.getenv("AGENT_FRAMEWORK_REVIEWER_MAX_REVISIONS", "1")),
            3,
        ),
    )

    # ------------------------------------------------------------------------------
    # 法令レイヤー別探索 vNext (docs/layered_legal_evidence_retrieval_plan.md)
    # 既定はshadowのみ。active切替はPhase 6の評価後に行う(§19)。
    # 上限値は §11.1 の初期値案であり、Phase 0の実測後に確定する。
    # ------------------------------------------------------------------------------
    agent_layered_legal_retrieval = os.getenv(
        "AGENT_LAYERED_LEGAL_RETRIEVAL", "false"
    ).lower() in {"1", "true", "yes", "on"}
    agent_layered_legal_retrieval_shadow = os.getenv(
        "AGENT_LAYERED_LEGAL_RETRIEVAL_SHADOW", "false"
    ).lower() in {"1", "true", "yes", "on"}
    layered_max_primary_issues = max(
        1, min(int(os.getenv("LAYERED_MAX_PRIMARY_ISSUES", "8")), 16)
    )
    layered_active_issue_batch_size = max(
        1, min(int(os.getenv("LAYERED_ACTIVE_ISSUE_BATCH_SIZE", "4")), 8)
    )
    layered_max_requirements_total = max(
        1, min(int(os.getenv("LAYERED_MAX_REQUIREMENTS_TOTAL", "24")), 96)
    )
    layered_max_legal_hops = max(
        1, min(int(os.getenv("LAYERED_MAX_LEGAL_HOPS", "3")), 5)
    )
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
    layered_max_replan_calls = max(
        0, min(int(os.getenv("LAYERED_MAX_REPLAN_CALLS", "1")), 2)
    )
    layered_shadow_phase_budget_sec = max(
        1, min(int(os.getenv("LAYERED_SHADOW_PHASE_BUDGET_SEC", "20")), 240)
    )
    layered_shadow_remaining_fraction = min(
        1.0, max(0.0, float(os.getenv("LAYERED_SHADOW_REMAINING_FRACTION", "0.5")))
    )
    # 採用profile名。Phase 0の実測後に明示する(§11.2)。未設定なら実値から推定する。
    agent_time_profile_name = os.getenv("AGENT_TIME_PROFILE_NAME", "")


settings = Settings()
