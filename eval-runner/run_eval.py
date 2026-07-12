import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
SAMPLES_DIR = Path(os.getenv("SAMPLES_DIR", "/workspace/samples"))
EVAL_RESULTS_DIR = Path(os.getenv("EVAL_RESULTS_DIR", "/workspace/eval-results"))
DEFAULT_LAWQA_PATH = SAMPLES_DIR / "eval" / "lawqa_eval_item.sample.jsonl"
LAWQA_EVAL_PATH = os.getenv("LAWQA_EVAL_PATH")
LAWQA_EVAL_URL = os.getenv("LAWQA_EVAL_URL")
EVAL_LIMIT = int(os.getenv("EVAL_LIMIT", "0") or "0")
EVAL_OFFSET = int(os.getenv("EVAL_OFFSET", "0") or "0")
EVAL_PATTERN = os.getenv("EVAL_PATTERN", "pattern_2_rule_based_agentic_rag")
REQUEST_TIMEOUT_SEC = int(os.getenv("REQUEST_TIMEOUT_SEC", "120"))
EVAL_SKIP_SEED = os.getenv("EVAL_SKIP_SEED", "false").lower() in {"1", "true", "yes", "on"}
EGOV_API_BASE_URL = os.getenv("EGOV_API_BASE_URL", "https://laws.e-gov.go.jp/api/1").rstrip("/")

CHOICE_LINE_PATTERN = re.compile(r"^([a-dA-D])[\s\u3000]+(.+)$")
EGOV_LAW_ID_PATTERN = re.compile(r"laws\.e-gov\.go\.jp/law/([^/?#]+)")
CONTEXT_HEADER_PATTERN = re.compile(r"^(#{2,5})\s+(.+?)\s*$")
ARTICLE_HEADER_PATTERN = re.compile(r"^\u7b2c(\d+)\u6761((?:\u306e\d+)*)")
PARAGRAPH_HEADER_PATTERN = re.compile(r"^\u7b2c(\d+)\u9805$")
ITEM_HEADER_PATTERN = re.compile(r"^\u7b2c(\d+)\u53f7$")
FULLWIDTH_DIGITS = str.maketrans("\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19", "0123456789")

# lawId -> e-Gov LawTitle \u306e\u30ad\u30e3\u30c3\u30b7\u30e5\u3002\u30b3\u30f3\u30c6\u30ad\u30b9\u30c8\u306e\u6cd5\u4ee4\u540d\u898b\u51fa\u3057\u3092 lawId \u3078\u5bfe\u5fdc\u4ed8\u3051\u308b\u305f\u3081\u306b\u4f7f\u3046\u3002
_EGOV_TITLE_CACHE: dict[str, str | None] = {}


def main() -> None:
    wait_for_api()
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
    citation_hit = sum(item["scores"].get("citationHit", 0) for item in results)
    summary = {
        "items": item_count,
        "answerAccuracy": answer_accuracy,
        "answerAccuracyRate": answer_accuracy / item_count if item_count else 0,
        "citationHit": citation_hit,
        "citationHitRate": citation_hit / item_count if item_count else 0,
        "citationLawHitRate": _optional_score_rate(results, "citationLawHit"),
        "citationArticleHitRate": _optional_score_rate(results, "citationArticleHit"),
        "citationParagraphHitRate": _optional_score_rate(results, "citationParagraphHit"),
        "llmUsed": sum(1 for item in results if item.get("llmUsed")),
        "validationErrors": sum(1 for item in results if item.get("validationError")),
        "source": results[0].get("source") if results else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def wait_for_api() -> None:
    for _ in range(60):
        try:
            response = requests.get(f"{API_URL}/health", timeout=5)
            if response.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError("Agent API did not become healthy")


def run_lawqa() -> list[dict[str, Any]]:
    rows, source = load_lawqa_rows()
    rows = rows[EVAL_OFFSET:]
    if EVAL_LIMIT > 0:
        rows = rows[:EVAL_LIMIT]
    results = []
    for row in rows:
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
            results.append(
                {
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
                    "scores": {
                        "answerAccuracy": 0,
                        "citationHit": 0,
                        "citationLawHit": None,
                        "citationArticleHit": None,
                        "citationParagraphHit": None,
                        "retrievalHitAt5": 0,
                        "graphExpansionHit": 0,
                    },
                    "latencyMs": None,
                }
            )
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
        citation_article_hit = (
            bool(expected_articles & retrieved_articles)
            if reference_granularity in {"article", "paragraph", "item"}
            else None
        )
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
        llm_trace = output.get("trace", {}).get("llm", {})
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
        results.append(
            {
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
                "scores": {
                    "answerAccuracy": 1 if output.get("predictedAnswer") == row["goldAnswer"] else 0,
                    "citationHit": 1 if citation_hit else 0,
                    "citationLawHit": _optional_binary(citation_law_hit),
                    "citationArticleHit": _optional_binary(citation_article_hit),
                    "citationParagraphHit": _optional_binary(citation_paragraph_hit),
                    "retrievalHitAt5": 1 if citation_hit else 0,
                    "graphExpansionHit": 1 if graph_expansion_hit else 0,
                },
                "latencyMs": None,
            }
        )
    return results


def load_lawqa_rows() -> tuple[list[dict[str, Any]], str]:
    if LAWQA_EVAL_URL:
        response = requests.get(LAWQA_EVAL_URL, timeout=REQUEST_TIMEOUT_SEC)
        response.raise_for_status()
        return normalize_lawqa_payload(response.json()), LAWQA_EVAL_URL

    path = Path(LAWQA_EVAL_PATH) if LAWQA_EVAL_PATH else DEFAULT_LAWQA_PATH
    if not path.exists():
        raise FileNotFoundError(f"lawqa eval file not found: {path}")
    if path.suffix == ".jsonl":
        return [normalize_internal_row(row, index) for index, row in enumerate(read_jsonl(path), start=1)], str(path)
    return normalize_lawqa_payload(json.loads(path.read_text(encoding="utf-8"))), str(path)


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


def _optional_binary(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _optional_score_rate(results: list[dict[str, Any]], key: str) -> float | None:
    values = [item["scores"].get(key) for item in results if item["scores"].get(key) is not None]
    return sum(values) / len(values) if values else None


def normalize_lawqa_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("samples"), list):
        samples = payload["samples"]
    elif isinstance(payload, list):
        samples = payload
    else:
        raise ValueError("Unsupported lawqa_jp JSON format. Expected list or object with samples list.")
    return [normalize_lawqa_sample(sample, index) for index, sample in enumerate(samples, start=1)]


def normalize_internal_row(row: dict[str, Any], index: int) -> dict[str, Any]:
    choices = {label.upper(): text for label, text in row["choices"].items()}
    return {
        **row,
        "questionId": row.get("questionId") or f"lawqa-{index:04d}",
        "choices": choices,
        "goldAnswer": str(row["goldAnswer"]).upper(),
    }


def normalize_lawqa_sample(sample: dict[str, Any], index: int) -> dict[str, Any]:
    filename = str(sample.get("ファイル名") or f"lawqa-{index:04d}")
    order = sample.get("回答オーダーマップ番号")
    question_id = filename if order is None else f"{filename}-{order}"
    references = [_reference_from_url(url) for url in sample.get("references", [])]
    law_ids = {ref["lawId"] for ref in references if ref.get("lawId")}
    # コンテキスト から条・項・号レベルの正解を補う（採点専用。API へは送らない）。
    references.extend(_context_expected_references(str(sample.get("コンテキスト") or ""), law_ids))
    return {
        "questionId": question_id,
        "question": str(sample["問題文"]),
        "choices": parse_choices(str(sample["選択肢"])),
        "goldAnswer": str(sample["output"]).upper(),
        "expectedReferences": references,
        "notes": "Converted from native lawqa_jp JSON. Context is not sent to Agent API.",
    }


def parse_choices(raw_choices: str) -> dict[str, str]:
    choices: dict[str, list[str]] = {}
    current_label: str | None = None
    for line in raw_choices.splitlines():
        line = line.strip()
        if not line:
            continue
        match = CHOICE_LINE_PATTERN.match(line)
        if match:
            current_label = match.group(1).upper()
            choices[current_label] = [match.group(2).strip()]
        elif current_label:
            choices[current_label].append(line)
    normalized = {label: "\n".join(parts).strip() for label, parts in choices.items()}
    missing = set("ABCD") - set(normalized)
    if missing:
        raise ValueError(f"Missing choices {sorted(missing)} in lawqa_jp row: {raw_choices[:120]}")
    return {label: normalized[label] for label in sorted(normalized)}


def _reference_from_url(url: str) -> dict[str, str]:
    reference = {"url": url}
    match = EGOV_LAW_ID_PATTERN.search(url)
    if match:
        reference["lawId"] = match.group(1)
    return reference


def _egov_title(law_id: str) -> str | None:
    """e-Gov から法令タイトルを取得する。失敗時は None（コンテキスト対応付けを諦め法令単位に劣化）。"""
    if law_id in _EGOV_TITLE_CACHE:
        return _EGOV_TITLE_CACHE[law_id]
    title: str | None = None
    try:
        response = requests.get(f"{EGOV_API_BASE_URL}/lawdata/{law_id}", timeout=60)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        title = root.findtext(".//LawTitle")
    except Exception:
        title = None
    _EGOV_TITLE_CACHE[law_id] = title
    return title


def _article_suffix(header: str) -> str | None:
    """'第2条の12' -> '2_12'、'第5条' -> '5'。seed.py の contentUnitId 生成規則に合わせる。"""
    match = ARTICLE_HEADER_PATTERN.match(header.translate(FULLWIDTH_DIGITS))
    if not match:
        return None
    parts = [match.group(1), *re.findall(r"の(\d+)", match.group(2))]
    return "_".join(parts)


def _pure_num(header: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.match(header.translate(FULLWIDTH_DIGITS))
    return int(match.group(1)) if match else None


def _context_expected_references(context: str, law_ids: set[str]) -> list[dict[str, str]]:
    """lawqa_jp の コンテキスト 見出し（## 法令名 / ### 第N条 / #### 第N項 / ##### 第N号）から
    条・項・号レベルの正解 contentUnitId を組み立てる。API へは送らず採点にのみ使う。"""
    title_to_law_id: dict[str, str] = {}
    for law_id in law_ids:
        title = _egov_title(law_id)
        if title:
            title_to_law_id.setdefault(title, law_id)
    if not title_to_law_id:
        return []

    references: list[dict[str, str]] = []
    seen: set[str] = set()
    current_law_id: str | None = None
    current_article: str | None = None
    current_paragraph: int | None = None
    for line in context.splitlines():
        header = CONTEXT_HEADER_PATTERN.match(line)
        if not header:
            continue
        level = len(header.group(1))
        text = header.group(2).strip()
        if level == 2:
            current_law_id = title_to_law_id.get(text)
            current_article = current_paragraph = None
        elif level == 3 and current_law_id:
            current_article = _article_suffix(text)
            current_paragraph = None
            if current_article:
                _add_reference(references, seen, current_law_id, f"law-{current_law_id}-article-{current_article}")
        elif level == 4 and current_law_id and current_article:
            current_paragraph = _pure_num(text, PARAGRAPH_HEADER_PATTERN)
            if current_paragraph is not None:
                content_unit_id = f"law-{current_law_id}-article-{current_article}-paragraph-{current_paragraph}"
                _add_reference(references, seen, current_law_id, content_unit_id)
        elif level == 5 and current_law_id and current_article and current_paragraph is not None:
            item_num = _pure_num(text, ITEM_HEADER_PATTERN)
            if item_num is not None:
                content_unit_id = (
                    f"law-{current_law_id}-article-{current_article}"
                    f"-paragraph-{current_paragraph}-item-{item_num}"
                )
                _add_reference(references, seen, current_law_id, content_unit_id)
    return references


def _add_reference(references: list[dict[str, str]], seen: set[str], law_id: str, content_unit_id: str) -> None:
    if content_unit_id in seen:
        return
    seen.add(content_unit_id)
    references.append({"lawId": law_id, "contentUnitId": content_unit_id})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
