import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from lawqa_jp import LawQADataset, answer_accuracy


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
_CONTAINER_SAMPLES_DIR = Path("/workspace/samples")
_LOCAL_SAMPLES_DIR = Path(__file__).resolve().parent.parent / "docs" / "samples"
SAMPLES_DIR = Path(
    os.getenv(
        "SAMPLES_DIR",
        str(_CONTAINER_SAMPLES_DIR if _CONTAINER_SAMPLES_DIR.exists() else _LOCAL_SAMPLES_DIR),
    )
)
EVAL_RESULTS_DIR = Path(os.getenv("EVAL_RESULTS_DIR", "/workspace/eval-results"))
LAWQA_EVAL_PATH = os.getenv("LAWQA_EVAL_PATH")
LAWQA_EVAL_URL = os.getenv("LAWQA_EVAL_URL")
EVAL_LIMIT = int(os.getenv("EVAL_LIMIT", "0") or "0")
EVAL_OFFSET = int(os.getenv("EVAL_OFFSET", "0") or "0")
EVAL_PATTERN = os.getenv("EVAL_PATTERN", "pattern_2_rule_based_agentic_rag")
REQUEST_TIMEOUT_SEC = int(os.getenv("REQUEST_TIMEOUT_SEC", "150"))
# agent wall timeに対して確保する安全マージン。これを下回るとクライアント側timeoutが
# agentの正常完了より先に発火し、性能集計が実態とずれる(§11.2)。
REQUEST_TIMEOUT_SAFETY_MARGIN_SEC = int(os.getenv("REQUEST_TIMEOUT_SAFETY_MARGIN_SEC", "10"))
EVAL_SKIP_SEED = os.getenv("EVAL_SKIP_SEED", "false").lower() in {"1", "true", "yes", "on"}
EGOV_API_BASE_URL = os.getenv("EGOV_API_BASE_URL", "https://laws.e-gov.go.jp/api/1").rstrip("/")

LAWQA_DATASET = LawQADataset(
    samples_dir=SAMPLES_DIR,
    eval_path=LAWQA_EVAL_PATH,
    eval_url=LAWQA_EVAL_URL,
    egov_api_base_url=EGOV_API_BASE_URL,
)
KNOWN_ISSUES = LAWQA_DATASET.known_issues
METRIC_VERSION = 6


def main() -> None:
    health = wait_for_api()
    assert_request_timeout_is_safe(health)
    if not EVAL_SKIP_SEED:
        requests.post(f"{API_URL}/admin/seed", timeout=120).raise_for_status()

    results: list[dict[str, Any]] = []
    results.extend(run_lawqa())

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EVAL_RESULTS_DIR / f"eval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with output_path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")

    print(f"Wrote {len(results)} eval results to {output_path}")
    item_count = len(results)
    answer_accuracy = sum(item["scores"].get("answerAccuracy", 0) for item in results)
    summary = {
        "metricVersion": METRIC_VERSION,
        "items": item_count,
        "answerAccuracy": answer_accuracy,
        "answerAccuracyRate": answer_accuracy / item_count if item_count else 0,
        "referenceScorable": sum(1 for item in results if item.get("referenceScorable")),
        # citationHit系は採点可能な問題(referenceScorable)だけで平均する
        "citationHitRate": _optional_score_rate(results, "citationHit"),
        "citationLawHitRate": _optional_score_rate(results, "citationLawHit"),
        "citationLawFamilyHitRate": _optional_score_rate(results, "citationLawFamilyHit"),
        "citationArticleHitRate": _optional_score_rate(results, "citationArticleHit"),
        "citationArticleCompleteHitRate": _optional_score_rate(results, "citationArticleCompleteHit"),
        "citationArticleRecall": _optional_score_rate(results, "citationArticleRecall"),
        "citationArticleMicroRecall": _article_micro_recall(results, "citationMatched"),
        "citationParagraphHitRate": _optional_score_rate(results, "citationParagraphHit"),
        "candidatePoolHitRate": _optional_score_rate(results, "candidatePoolHit"),
        "candidatePoolArticleCompleteHitRate": _optional_score_rate(
            results, "candidatePoolArticleCompleteHit"
        ),
        "candidatePoolArticleRecall": _optional_score_rate(results, "candidatePoolArticleRecall"),
        "candidatePoolArticleMicroRecall": _article_micro_recall(
            results, "candidatePoolMatched"
        ),
        "fusionHitRate": _optional_score_rate(results, "fusionHit"),
        "fusionArticleCompleteHitRate": _optional_score_rate(results, "fusionArticleCompleteHit"),
        "fusionArticleRecall": _optional_score_rate(results, "fusionArticleRecall"),
        "fusionArticleMicroRecall": _article_micro_recall(results, "fusionMatched"),
        "rerankerHitRate": _optional_score_rate(results, "rerankerHit"),
        "rerankerArticleCompleteHitRate": _optional_score_rate(
            results, "rerankerArticleCompleteHit"
        ),
        "rerankerArticleRecall": _optional_score_rate(results, "rerankerArticleRecall"),
        "rerankerArticleMicroRecall": _article_micro_recall(results, "rerankerMatched"),
        "shadowRerankerArticleCompleteHitRate": _optional_score_rate(
            results, "shadowRerankerArticleCompleteHit"
        ),
        "shadowRerankerArticleRecall": _optional_score_rate(
            results, "shadowRerankerArticleRecall"
        ),
        "shadowRerankerArticleMicroRecall": _article_micro_recall(
            results, "shadowRerankerMatched"
        ),
        "layeredContextArticleCompleteHitRate": _optional_score_rate(
            results, "layeredContextArticleCompleteHit"
        ),
        "layeredContextArticleRecall": _optional_score_rate(
            results, "layeredContextArticleRecall"
        ),
        "layeredContextArticleMicroRecall": _article_micro_recall(
            results, "layeredContextMatched"
        ),
        "primaryConclusionGroupCompleteRate": _group_micro_rate(
            results, "primaryIncluded", "primaryTotal"
        ),
        "mandatoryConclusionGroupCompleteRate": _group_micro_rate(
            results, "mandatoryIncluded", "mandatoryTotal"
        ),
        "layeredAnswerStatusCounts": _value_counts(results, "answerStatus"),
        "layeredShadowIncomplete": sum(
            1
            for item in results
            if (item.get("layeredLegalRetrieval") or {}).get("shadowIncomplete")
        ),
        "shadowSelectionComplete": sum(
            1
            for item in results
            if (item.get("shadowSelection") or {}).get("complete")
        ),
        "shadowSelectionIncomplete": sum(
            1
            for item in results
            if item.get("shadowSelection")
            and not item["shadowSelection"].get("complete")
        ),
        "rerankerUsed": sum(1 for item in results if item.get("rerankerUsed")),
        "llmUsed": sum(1 for item in results if item.get("llmUsed")),
        "validationErrors": sum(1 for item in results if item.get("validationError")),
        "knownDatasetIssues": sum(1 for item in results if item.get("datasetIssue")),
        "source": results[0].get("source") if results else None,
    }
    diagnostic_results = [item for item in results if item.get("diagnosticScorable", True)]
    summary["diagnosticScorable"] = len(diagnostic_results)
    summary["diagnosticAnswerAccuracy"] = sum(
        item["scores"].get("answerAccuracy", 0) for item in diagnostic_results
    )
    summary["diagnosticAnswerAccuracyRate"] = (
        summary["diagnosticAnswerAccuracy"] / len(diagnostic_results) if diagnostic_results else None
    )
    summary["diagnosticRerankerArticleCompleteHitRate"] = _optional_score_rate(
        diagnostic_results,
        "rerankerArticleCompleteHit",
    )
    summary["diagnosticRerankerArticleRecall"] = _optional_score_rate(
        diagnostic_results,
        "rerankerArticleRecall",
    )
    summary["diagnosticRerankerArticleMicroRecall"] = _article_micro_recall(
        diagnostic_results,
        "rerankerMatched",
    )
    summary["diagnosticShadowRerankerArticleCompleteHitRate"] = _optional_score_rate(
        diagnostic_results,
        "shadowRerankerArticleCompleteHit",
    )
    summary["diagnosticShadowRerankerArticleRecall"] = _optional_score_rate(
        diagnostic_results,
        "shadowRerankerArticleRecall",
    )
    summary["diagnosticShadowRerankerArticleMicroRecall"] = _article_micro_recall(
        diagnostic_results,
        "shadowRerankerMatched",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def wait_for_api() -> dict[str, Any]:
    for _ in range(60):
        try:
            response = requests.get(f"{API_URL}/health", timeout=5)
            if response.ok:
                return response.json()
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Agent API did not become healthy")


def assert_request_timeout_is_safe(health: dict[str, Any]) -> None:
    """agent wall timeより短いREQUEST_TIMEOUT_SECで評価を開始しない。

    REQUEST_TIMEOUT_SECはeval-runner側の設定であり、agent-api単独では比較できないため、
    /healthが公開するwall timeと突き合わせる
    (docs/layered_legal_evidence_retrieval_plan.md §11.2)。
    """
    budget = (health or {}).get("timeBudget") or {}
    wall_time = budget.get("agentMaxWallTimeSec")
    if not isinstance(wall_time, (int, float)):
        print("[eval] warning: /health does not expose timeBudget.agentMaxWallTimeSec")
        return
    print(
        f"[eval] agent time profile={budget.get('profileName')} "
        f"wallTime={wall_time}s requestTimeout={REQUEST_TIMEOUT_SEC}s"
    )
    for warning in budget.get("warnings") or []:
        print(f"[eval] agent time budget warning: {warning}")
    if REQUEST_TIMEOUT_SEC <= wall_time + REQUEST_TIMEOUT_SAFETY_MARGIN_SEC:
        raise RuntimeError(
            "REQUEST_TIMEOUT_SEC="
            f"{REQUEST_TIMEOUT_SEC}s is not longer than agent wall time {wall_time}s "
            f"+ safety margin {REQUEST_TIMEOUT_SAFETY_MARGIN_SEC}s. "
            "評価を開始せず設定エラーとする。"
        )


def _print_question_result(index: int, total: int, result: dict[str, Any]) -> None:
    """1問ごとの結果を人が読める1行でターミナルへ出す。集計JSONだけだと
    予測/正解が分からないため、手動での1問確認を分かりやすくする。"""
    scores = result.get("scores", {})
    mark = "○" if scores.get("answerAccuracy") == 1 else "×"
    predicted = result.get("predictedAnswer") or "-"
    gold = result.get("goldAnswer") or "-"
    hit_label = {1: "一致", 0: "不一致", None: "-"}.get(scores.get("citationArticleHit"), "-")
    llm = "LLM使用" if result.get("llmUsed") else "LLM未使用"
    print(
        f"[{index}/{total}] {mark} {result.get('questionId')}  "
        f"予測={predicted} 正解={gold}  引用条文={hit_label}  ({llm})",
        flush=True,
    )


def run_lawqa() -> list[dict[str, Any]]:
    rows, source = load_lawqa_rows()
    rows = rows[EVAL_OFFSET:]
    if EVAL_LIMIT > 0:
        rows = rows[:EVAL_LIMIT]
    results = []
    total = len(rows)
    for index, row in enumerate(rows, start=1):
        try:
            response = requests.post(
                f"{API_URL}/answer",
                json={
                    "question": row["question"],
                    "choices": row["choices"],
                    "pattern": EVAL_PATTERN,
                    "userClearanceLevel": 2,
                },
                timeout=REQUEST_TIMEOUT_SEC,
            )
            response.raise_for_status()
            output = response.json()
        except requests.RequestException as exc:
            # 1問の一時的な失敗（Anthropic側の瞬断等）で140問全体を止めない。
            # 失敗を記録して次の問題へ進む。
            print(f"SKIP {row['questionId']}: request failed: {exc}")
            dataset_issue = KNOWN_ISSUES.get(row["questionId"])
            results.append(
                {
                    "metricVersion": METRIC_VERSION,
                    "runId": f"run-{row['questionId']}",
                    "pattern": EVAL_PATTERN,
                    "dataset": "lawqa_jp",
                    "source": source,
                    "questionId": row["questionId"],
                    "referenceGranularity": None,
                    "inputType": "multiple_choice_legal_qa",
                    "searchPlan": [],
                    "toolCalls": [],
                    "retrievedContentUnitIds": [],
                    "retrievedGraphNodeIds": [],
                    "retrievedGraphEdgeIds": [],
                    "citations": [],
                    "predictedAnswer": None,
                    "goldAnswer": row["goldAnswer"],
                    "llmUsed": False,
                    "validationError": None,
                    "llmError": f"request_failed: {exc}",
                    "datasetIssue": dataset_issue,
                    "diagnosticScorable": not bool(
                        dataset_issue and dataset_issue.get("excludeFromDiagnosticAccuracy")
                    ),
                    "articleCoverage": None,
                    "scores": {
                        "answerAccuracy": 0,
                        "citationHit": None,
                        "citationLawHit": None,
                        "citationLawFamilyHit": None,
                        "citationArticleHit": None,
                        "citationArticleCompleteHit": None,
                        "citationArticleRecall": None,
                        "citationParagraphHit": None,
                        "candidatePoolHit": None,
                        "candidatePoolArticleCompleteHit": None,
                        "candidatePoolArticleRecall": None,
                        "fusionHit": None,
                        "fusionArticleCompleteHit": None,
                        "fusionArticleRecall": None,
                        "rerankerHit": None,
                        "rerankerArticleCompleteHit": None,
                        "rerankerArticleRecall": None,
                        "shadowRerankerHit": None,
                        "shadowRerankerArticleCompleteHit": None,
                        "shadowRerankerArticleRecall": None,
                        "graphExpansionHit": 0,
                    },
                    "shadowSelection": None,
                    "latencyMs": None,
                }
            )
            _print_question_result(index, total, results[-1])
            continue
        expected = {ref["contentUnitId"] for ref in row.get("expectedReferences", []) if ref.get("contentUnitId")}
        expected_law_ids = {ref["lawId"] for ref in row.get("expectedReferences", []) if ref.get("lawId")}
        expected_document_ids = {f"law-{law_id}" for law_id in expected_law_ids}
        retrieved = {citation.get("contentUnitId") for citation in output.get("citations", [])}
        retrieved_document_ids = {citation.get("documentId") for citation in output.get("citations", [])}
        retrieved_parents = {
            str(citation.get("contentUnitId") or "").rsplit("-paragraph-", 1)[0]
            for citation in output.get("citations", [])
        }
        reference_granularity = _reference_granularity(expected)
        citation_law_hit = bool(expected_document_ids & retrieved_document_ids) if expected_document_ids else None
        expected_articles = {_article_content_unit_id(content_unit_id) for content_unit_id in expected}
        retrieved_articles = {_article_content_unit_id(content_unit_id) for content_unit_id in retrieved if content_unit_id}
        if reference_granularity in {"article", "paragraph", "item"}:
            citation_article_hit, citation_article_complete_hit, citation_article_recall = (
                _article_coverage(expected_articles, retrieved_articles)
            )
            citation_matched = len(expected_articles & retrieved_articles)
        else:
            citation_article_hit = citation_article_complete_hit = citation_article_recall = None
            citation_matched = None
        citation_paragraph_hit = (
            bool(expected & retrieved or expected & retrieved_parents)
            if reference_granularity in {"paragraph", "item"}
            else None
        )
        citation_hit = (
            citation_law_hit
            if reference_granularity == "law"
            else citation_article_hit
        )
        # 期待参照が全く無い問題(非e-Gov PDFのみ等)は検索精度を採点できない。
        reference_scorable = bool(expected_document_ids or expected)

        # 法令ファミリー一致: 親法を期待して委任法令(施行令・府令)を引いたケースを別軸で計測。
        expected_families = {_family_of(document_id) for document_id in expected_document_ids}
        retrieved_families = {_family_of(document_id) for document_id in retrieved_document_ids if document_id}
        citation_law_family_hit = bool(expected_families & retrieved_families) if expected_document_ids else None

        # 候補プール→RRF融合上位→最終引用のどこで期待根拠を落としたかを分離。
        trace = output.get("trace", {})
        layered_trace = trace.get("layeredLegalRetrieval") or {}
        layered_context_ids = set(layered_trace.get("newContextContentUnitIds") or [])
        (
            layered_context_hit,
            layered_context_complete_hit,
            layered_context_recall,
            layered_context_matched,
        ) = _article_scores_at(
            layered_context_ids,
            reference_scorable,
            reference_granularity,
            expected_document_ids,
            expected_articles,
        ) if layered_trace else (None, None, None, None)
        layered_group_coverage = _layered_group_coverage(layered_trace)
        candidate_ids = set(trace.get("candidatePoolContentUnitIds", trace.get("retrievedContentUnitIds", [])))
        fusion_ids = set(trace.get("fusionTopContentUnitIds", []))
        # v5では新方式を有効化した実行でも旧16件を比較基準として保持する。
        # oldContextContentUnitIds がない旧traceだけ従来キーへフォールバックする。
        reranker_ids = _old_context_ids(trace, fusion_ids)
        shadow_selection = trace.get("shadowSelection")
        shadow_ids = set(trace.get("newContextContentUnitIds", []))

        candidate_pool_hit, candidate_complete_hit, candidate_recall, candidate_matched = (
            _article_scores_at(
                candidate_ids,
                reference_scorable,
                reference_granularity,
                expected_document_ids,
                expected_articles,
            )
        )
        fusion_hit, fusion_complete_hit, fusion_recall, fusion_matched = (
            _article_scores_at(
                fusion_ids,
                reference_scorable,
                reference_granularity,
                expected_document_ids,
                expected_articles,
            )
        )
        reranker_hit, reranker_complete_hit, reranker_recall, reranker_matched = (
            _article_scores_at(
                reranker_ids,
                reference_scorable,
                reference_granularity,
                expected_document_ids,
                expected_articles,
            )
        )
        if shadow_selection and shadow_selection.get("complete"):
            (
                shadow_reranker_hit,
                shadow_reranker_complete_hit,
                shadow_reranker_recall,
                shadow_reranker_matched,
            ) = _article_scores_at(
                shadow_ids,
                reference_scorable,
                reference_granularity,
                expected_document_ids,
                expected_articles,
            )
        else:
            (
                shadow_reranker_hit,
                shadow_reranker_complete_hit,
                shadow_reranker_recall,
                shadow_reranker_matched,
            ) = (None, None, None, None)
        llm_trace = trace.get("llm", {})
        planner_trace = trace.get("planner", {})
        evaluator_trace = trace.get("evaluator", {})
        reranker_trace = trace.get("reranker", {})
        llm_used = bool(llm_trace.get("used"))
        validation_error = llm_trace.get("validationError")
        llm_error = llm_trace.get("error")
        retrieved_graph_node_ids = output.get("trace", {}).get("retrievedGraphNodeIds", [])
        retrieved_graph_edge_ids = output.get("trace", {}).get("retrievedGraphEdgeIds", [])
        graph_expanded = set(output.get("trace", {}).get("graphExpandedContentUnitIds", []))
        graph_parents = {
            str(content_unit_id).rsplit("-paragraph-", 1)[0]
            for content_unit_id in graph_expanded
        }
        graph_document_ids = {
            str(content_unit_id).split("-article-", 1)[0]
            for content_unit_id in graph_expanded
        }
        graph_expansion_hit = bool(
            expected & graph_expanded
            or expected & graph_parents
            or expected_document_ids & graph_document_ids
        )
        dataset_issue = KNOWN_ISSUES.get(row["questionId"])
        diagnostic_scorable = not bool(
            dataset_issue and dataset_issue.get("excludeFromDiagnosticAccuracy")
        )
        results.append(
            {
                "metricVersion": METRIC_VERSION,
                "runId": f"run-{row['questionId']}",
                "pattern": output["pattern"],
                "dataset": "lawqa_jp",
                "source": source,
                "questionId": row["questionId"],
                "referenceGranularity": reference_granularity,
                "inputType": "multiple_choice_legal_qa",
                "searchPlan": output["route"],
                "toolCalls": output["trace"].get("rounds", []),
                "retrievedContentUnitIds": list(retrieved),
                "retrievedGraphNodeIds": retrieved_graph_node_ids,
                "retrievedGraphEdgeIds": retrieved_graph_edge_ids,
                "citations": output["citations"],
                "predictedAnswer": output.get("predictedAnswer"),
                "goldAnswer": row["goldAnswer"],
                "llmUsed": llm_used,
                "validationError": validation_error,
                "llmError": llm_error,
                "referenceScorable": reference_scorable,
                "datasetIssue": dataset_issue,
                "diagnosticScorable": diagnostic_scorable,
                "articleCoverage": (
                    {
                        "expected": len(expected_articles),
                        "citationMatched": citation_matched,
                        "candidatePoolMatched": candidate_matched,
                        "fusionMatched": fusion_matched,
                        "rerankerMatched": reranker_matched,
                        "shadowRerankerMatched": shadow_reranker_matched,
                        "layeredContextMatched": layered_context_matched,
                    }
                    if citation_matched is not None
                    else None
                ),
                "rerankerUsed": bool(reranker_trace.get("used")),
                "rerankerModel": reranker_trace.get("model"),
                "rerankerLatencyMs": reranker_trace.get("latencyMs"),
                "rerankerError": reranker_trace.get("error"),
                "llmCallCount": trace.get("llmCallCount"),
                "elapsedMs": trace.get("elapsedMs"),
                "agentStopReason": trace.get("stopReason"),
                "answerLlmStopReason": llm_trace.get("stopReason"),
                "plannerStopReason": planner_trace.get("stopReason"),
                "evaluatorStopReason": evaluator_trace.get("stopReason"),
                "shadowSelection": (
                    {
                        **shadow_selection,
                        "oldContextContentUnitIds": trace.get(
                            "oldContextContentUnitIds",
                            [],
                        ),
                        "newContextContentUnitIds": trace.get(
                            "newContextContentUnitIds",
                            [],
                        ),
                        "bestAspectCandidateMissingFrom30": trace.get(
                            "bestAspectCandidateMissingFrom30",
                            [],
                        ),
                        "graphInheritedCandidateMissingFrom30": trace.get(
                            "graphInheritedCandidateMissingFrom30",
                            [],
                        ),
                        "skippedAspectQueries": trace.get(
                            "skippedAspectQueries",
                            [],
                        ),
                        "aspectPhaseBudgetMs": trace.get("aspectPhaseBudgetMs"),
                        "aspectPhaseElapsedMs": trace.get("aspectPhaseElapsedMs"),
                    }
                    if shadow_selection
                    else None
                ),
                "layeredLegalRetrieval": layered_trace or None,
                "answerStatus": (
                    (layered_trace.get("answerControl") or {}).get("answerStatus")
                    or (layered_trace.get("contextCoverage") or {}).get("answerStatus")
                ),
                "layeredGroupCoverage": layered_group_coverage,
                "contextTruncation": layered_trace.get("contextTruncation"),
                "questionPolarity": llm_trace.get("questionPolarity"),
                "choiceAssessments": llm_trace.get("choiceAssessments"),
                "scores": {
                    "answerAccuracy": answer_accuracy(
                        output.get("predictedAnswer"), row["goldAnswer"]
                    ),
                    "citationHit": _optional_binary(citation_hit),
                    "citationLawHit": _optional_binary(citation_law_hit),
                    "citationLawFamilyHit": _optional_binary(citation_law_family_hit),
                    "citationArticleHit": _optional_binary(citation_article_hit),
                    "citationArticleCompleteHit": _optional_binary(citation_article_complete_hit),
                    "citationArticleRecall": citation_article_recall,
                    "citationParagraphHit": _optional_binary(citation_paragraph_hit),
                    "candidatePoolHit": _optional_binary(candidate_pool_hit),
                    "candidatePoolArticleCompleteHit": _optional_binary(candidate_complete_hit),
                    "candidatePoolArticleRecall": candidate_recall,
                    "fusionHit": _optional_binary(fusion_hit),
                    "fusionArticleCompleteHit": _optional_binary(fusion_complete_hit),
                    "fusionArticleRecall": fusion_recall,
                    "rerankerHit": _optional_binary(reranker_hit),
                    "rerankerArticleCompleteHit": _optional_binary(reranker_complete_hit),
                    "rerankerArticleRecall": reranker_recall,
                    "shadowRerankerHit": _optional_binary(shadow_reranker_hit),
                    "shadowRerankerArticleCompleteHit": _optional_binary(
                        shadow_reranker_complete_hit
                    ),
                    "shadowRerankerArticleRecall": shadow_reranker_recall,
                    "layeredContextHit": _optional_binary(layered_context_hit),
                    "layeredContextArticleCompleteHit": _optional_binary(
                        layered_context_complete_hit
                    ),
                    "layeredContextArticleRecall": layered_context_recall,
                    "graphExpansionHit": 1 if graph_expansion_hit else 0,
                },
                "latencyMs": output.get("trace", {}).get("elapsedMs"),
            }
        )
        _print_question_result(index, total, results[-1])
    return results


def load_lawqa_rows() -> tuple[list[dict[str, Any]], str]:
    return LAWQA_DATASET.load(timeout_sec=REQUEST_TIMEOUT_SEC)


def _reference_granularity(expected: set[str]) -> str:
    if any("-item-" in content_unit_id for content_unit_id in expected):
        return "item"
    if any("-paragraph-" in content_unit_id for content_unit_id in expected):
        return "paragraph"
    if expected:
        return "article"
    return "law"


def _article_content_unit_id(content_unit_id: str) -> str:
    return content_unit_id.split("-paragraph-", 1)[0]


def _article_coverage(
    expected_articles: set[str],
    retrieved_articles: set[str],
) -> tuple[bool | None, bool | None, float | None]:
    if not expected_articles:
        return None, None, None
    overlap = expected_articles & retrieved_articles
    return bool(overlap), expected_articles <= retrieved_articles, len(overlap) / len(expected_articles)


def _article_scores_at(
    ids: set[str],
    reference_scorable: bool,
    reference_granularity: str,
    expected_document_ids: set[str],
    expected_articles: set[str],
) -> tuple[bool | None, bool | None, float | None, int | None]:
    if not reference_scorable:
        return None, None, None, None
    if reference_granularity == "law":
        law_hit = bool(
            expected_document_ids
            & {_document_id_of(item) for item in ids if item}
        )
        return law_hit, None, None, None
    articles = {_article_content_unit_id(item) for item in ids if item}
    any_hit, complete_hit, recall = _article_coverage(
        expected_articles,
        articles,
    )
    return any_hit, complete_hit, recall, len(expected_articles & articles)


def _old_context_ids(
    trace: dict[str, Any],
    fallback_ids: set[str],
) -> set[str]:
    """新方式の有効化後も、比較基準には変更前の16件を使う。"""
    return set(
        trace.get("oldContextContentUnitIds")
        or trace.get("rerankerTopContentUnitIds", fallback_ids)
    )


def _document_id_of(content_unit_id: str) -> str:
    """contentUnitId から documentId(law-<法令番号>)を取り出す。附則(suppl)IDにも対応。"""
    return "-".join(str(content_unit_id).split("-")[:2])


def _family_of(document_id: str) -> str:
    """documentId を法令ファミリーの親法IDへ丸める(施行令・府令→親法)。"""
    return LAWQA_DATASET.family_of(document_id)


def _optional_binary(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _optional_score_rate(results: list[dict[str, Any]], key: str) -> float | None:
    values = [item["scores"].get(key) for item in results if item["scores"].get(key) is not None]
    return sum(values) / len(values) if values else None


def _article_micro_recall(results: list[dict[str, Any]], matched_key: str) -> float | None:
    coverages = [
        item["articleCoverage"]
        for item in results
        if item.get("articleCoverage")
        and item["articleCoverage"].get(matched_key) is not None
    ]
    expected = sum(item["expected"] for item in coverages)
    matched = sum(item[matched_key] for item in coverages)
    return matched / expected if expected else None


def _layered_group_coverage(layered_trace: dict[str, Any]) -> dict[str, Any] | None:
    if not layered_trace:
        return None
    coverage = layered_trace.get("contextCoverage") or {}
    primary = set(coverage.get("primaryConclusionGroupIds") or [])
    primary_included = set(coverage.get("includedPrimaryConclusionGroupIds") or [])
    mandatory_included = set(coverage.get("includedConclusionGroupIds") or [])
    mandatory_omitted = set(coverage.get("omittedConclusionGroupIds") or [])
    mandatory = mandatory_included | mandatory_omitted
    return {
        "primaryTotal": len(primary),
        "primaryIncluded": len(primary & primary_included),
        "mandatoryTotal": len(mandatory),
        "mandatoryIncluded": len(mandatory_included),
        "additionalChunksNeeded": int(coverage.get("additionalChunksNeeded") or 0),
    }


def _group_micro_rate(
    results: list[dict[str, Any]],
    included_key: str,
    total_key: str,
) -> float | None:
    coverages = [
        item["layeredGroupCoverage"]
        for item in results
        if item.get("layeredGroupCoverage")
    ]
    total = sum(int(item.get(total_key) or 0) for item in coverages)
    included = sum(int(item.get(included_key) or 0) for item in coverages)
    return included / total if total else None


def _value_counts(results: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        value = item.get(key)
        if value is not None:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


if __name__ == "__main__":
    main()
