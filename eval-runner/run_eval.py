import json
import os
import re
import time
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

CHOICE_LINE_PATTERN = re.compile(r"^([a-dA-D])[\s\u3000]+(.+)$")
EGOV_LAW_ID_PATTERN = re.compile(r"laws\.e-gov\.go\.jp/law/([^/?#]+)")


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
        expected = {ref["contentUnitId"] for ref in row.get("expectedReferences", []) if ref.get("contentUnitId")}
        expected_law_ids = {ref["lawId"] for ref in row.get("expectedReferences", []) if ref.get("lawId")}
        expected_document_ids = {f"law-{law_id}" for law_id in expected_law_ids}
        retrieved = {citation.get("contentUnitId") for citation in output.get("citations", [])}
        retrieved_document_ids = {citation.get("documentId") for citation in output.get("citations", [])}
        retrieved_parents = {
            str(citation.get("contentUnitId") or "").rsplit("-paragraph-", 1)[0]
            for citation in output.get("citations", [])
        }
        citation_hit = bool(
            expected & retrieved
            or expected & retrieved_parents
            or expected_document_ids & retrieved_document_ids
        )
        llm_trace = output.get("trace", {}).get("llm", {})
        llm_used = bool(llm_trace.get("used"))
        validation_error = llm_trace.get("validationError")
        llm_error = llm_trace.get("error")
        results.append(
            {
                "runId": f"run-{row['questionId']}",
                "pattern": output["pattern"],
                "dataset": "lawqa_jp",
                "source": source,
                "questionId": row["questionId"],
                "inputType": "multiple_choice_legal_qa",
                "searchPlan": output["route"],
                "toolCalls": output["trace"].get("rounds", []),
                "retrievedContentUnitIds": list(retrieved),
                "retrievedGraphNodeIds": [],
                "retrievedGraphEdgeIds": [],
                "citations": output["citations"],
                "predictedAnswer": output.get("predictedAnswer"),
                "goldAnswer": row["goldAnswer"],
                "llmUsed": llm_used,
                "validationError": validation_error,
                "llmError": llm_error,
                "scores": {
                    "answerAccuracy": 1 if output.get("predictedAnswer") == row["goldAnswer"] else 0,
                    "citationHit": 1 if citation_hit else 0,
                    "retrievalHitAt5": 1 if citation_hit else 0,
                    "graphExpansionHit": 0,
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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
