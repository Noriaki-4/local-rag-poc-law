"""UIの質問例12問をAgent APIへ送り、資料・必要条文・回答要点を確認する回帰チェック。

lawqa_jpの選択式評価では測れない「自然言語の質問で必要な資料へ到達できるか」を見る。
採点基準はAPIレスポンスを受け取った後にだけ使い、検索・回答生成には渡さない。

使い方:
    uv run --project agent-ui python scripts/check_example_questions.py
    AGENT_API_URL=http://localhost:8000 TOP_K=8 RUNS=1 で調整可能。

検索とLLMには揺らぎがあるため、1回の結果で合否を判断しないこと。RUNS を増やして
到達率で見る。終了コードは、全問が資料・必要条文・回答要点を全て満たした場合のみ0。
"""

import json
import os
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent-ui"))

from example_questions import EXAMPLE_QUESTIONS, evaluate_example  # noqa: E402

API_URL = os.getenv("AGENT_API_URL", "http://localhost:8000").rstrip("/")
TOP_K = int(os.getenv("TOP_K", "8"))
RUNS = int(os.getenv("RUNS", "1"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))
TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", "600"))
RESULTS_PATH = os.getenv("EXAMPLE_RESULTS_PATH", "").strip()
TITLE_FILTER = {
    title.strip()
    for title in os.getenv("EXAMPLE_TITLES", "").split(",")
    if title.strip()
}


def build_request_payload(example) -> dict:
    """採点情報を含めず、利用者が入力した質問と通常設定だけをAPIへ渡す。"""
    return {
        "question": example.question,
        "pattern": os.getenv("PATTERN", "pattern_4_deepsearch"),
        "topK": TOP_K,
        "userClearanceLevel": 2,
    }


def ask(example) -> dict:
    payload = json.dumps(build_request_payload(example)).encode()
    request = urllib.request.Request(
        f"{API_URL}/answer", data=payload, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            result = json.load(response)
    except Exception as exc:  # noqa: BLE001
        return {"example": example, "error": str(exc)}
    citations = result.get("citations", [])
    return {
        "example": example,
        "evaluation": evaluate_example(example, citations, result.get("answer")),
        "citedTitles": sorted({c.get("title") for c in citations if c.get("title")}),
        "response": result,
    }


def main() -> int:
    reached_counter: Counter = Counter()
    failures = 0
    saved_rows = []
    examples = tuple(
        example
        for example in EXAMPLE_QUESTIONS
        if not TITLE_FILTER or example.title in TITLE_FILTER
    )
    if not examples:
        print("ERROR: EXAMPLE_TITLES に一致する例題がありません。")
        return 2
    for run in range(RUNS):
        if RUNS > 1:
            print(f"--- {run + 1}回目")
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for outcome in pool.map(ask, examples):
                example = outcome["example"]
                if outcome.get("error"):
                    failures += 1
                    print(f"ERROR Lv.{example.level} {example.title}: {outcome['error']}")
                    saved_rows.append(
                        {
                            "run": run + 1,
                            "level": example.level,
                            "title": example.title,
                            "question": example.question,
                            "legalAsOf": example.legal_as_of,
                            "error": outcome["error"],
                        }
                    )
                    continue
                evaluation = outcome["evaluation"]
                reached_counter[example.title] += evaluation.passed
                if not evaluation.passed:
                    failures += 1
                mark = "OK  " if evaluation.passed else "MISS"
                grouped_statuses = (
                    ("資料", evaluation.source_statuses),
                    ("条文", evaluation.evidence_statuses),
                    ("要点", evaluation.answer_point_statuses),
                )
                reached_count = sum(
                    status.reached for _, statuses in grouped_statuses for status in statuses
                )
                status_count = sum(len(statuses) for _, statuses in grouped_statuses)
                print(
                    f"{mark} Lv.{example.level} {example.title} "
                    f"({reached_count}/{status_count})"
                )
                print(f"     期待: {example.expected}")
                print(f"     引用: {'、'.join(outcome['citedTitles']) or 'なし'}")
                for category, statuses in grouped_statuses:
                    missing = "、".join(status.name for status in statuses if not status.reached)
                    if missing:
                        print(f"     未確認の{category}: {missing}")
                saved_rows.append(
                    {
                        "run": run + 1,
                        "level": example.level,
                        "title": example.title,
                        "question": example.question,
                        "legalAsOf": example.legal_as_of,
                        "passed": evaluation.passed,
                        "sourceStatuses": [status._asdict() for status in evaluation.source_statuses],
                        "evidenceStatuses": [
                            status._asdict() for status in evaluation.evidence_statuses
                        ],
                        "answerPointStatuses": [
                            status._asdict() for status in evaluation.answer_point_statuses
                        ],
                        "response": outcome["response"],
                    }
                )

    if RUNS > 1:
        print("\n--- 全到達した回数 / 実行回数")
        for example in examples:
            print(f"  {reached_counter[example.title]}/{RUNS} Lv.{example.level} {example.title}")
    if RESULTS_PATH:
        output_path = Path(RESULTS_PATH)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as stream:
            for row in saved_rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\nWrote {len(saved_rows)} results to {output_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
