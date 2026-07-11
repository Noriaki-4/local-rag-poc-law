import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
SAMPLES_DIR = Path(os.getenv("SAMPLES_DIR", "/workspace/samples"))
EVAL_RESULTS_DIR = Path(os.getenv("EVAL_RESULTS_DIR", "/workspace/eval-results"))


def main() -> None:
    wait_for_api()
    requests.post(f"{API_URL}/admin/seed", timeout=120).raise_for_status()

    results: list[dict[str, Any]] = []
    results.extend(run_lawqa())

    EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = EVAL_RESULTS_DIR / f"eval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with output_path.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")

    print(f"Wrote {len(results)} eval results to {output_path}")
    summary = {
        "items": len(results),
        "answerAccuracy": sum(item["scores"].get("answerAccuracy", 0) for item in results),
        "citationHit": sum(item["scores"].get("citationHit", 0) for item in results),
        "llmUsed": sum(1 for item in results if item.get("llmUsed")),
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
    path = SAMPLES_DIR / "eval" / "lawqa_eval_item.sample.jsonl"
    rows = read_jsonl(path)
    results = []
    for row in rows:
        response = requests.post(
            f"{API_URL}/answer",
            json={
                "question": row["question"],
                "choices": row["choices"],
                "pattern": "pattern_2_rule_based_agentic_rag",
                "userClearanceLevel": 2,
            },
            timeout=120,
        )
        response.raise_for_status()
        output = response.json()
        expected = {ref["contentUnitId"] for ref in row.get("expectedReferences", [])}
        retrieved = {citation.get("contentUnitId") for citation in output.get("citations", [])}
        retrieved_parents = {
            str(citation.get("contentUnitId") or "").rsplit("-paragraph-", 1)[0]
            for citation in output.get("citations", [])
        }
        citation_hit = bool(expected & retrieved or expected & retrieved_parents)
        llm_used = bool(output.get("trace", {}).get("llm", {}).get("used"))
        results.append(
            {
                "runId": f"run-{row['questionId']}",
                "pattern": output["pattern"],
                "dataset": "lawqa_jp",
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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
