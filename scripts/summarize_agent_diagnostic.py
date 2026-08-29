#!/usr/bin/env python3
"""診断JSONLから、LLMを呼ばずにCycle監査報告を生成する。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-api"))

from app.agent_framework.cycle_audit import (  # noqa: E402
    build_cycle_audit_report,
    render_cycle_audit_markdown,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agent診断JSONLをCycle単位のJSON/Markdownへ要約します。",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _load_records(path: Path) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}") from exc
        if not isinstance(value, dict):
            raise TypeError(f"diagnostic record {line_number} is not an object")
        records.append(value)
    return tuple(records)


def main() -> int:
    args = _parse_args()
    report = build_cycle_audit_report(_load_records(args.input))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "cycle-audit.json"
    markdown_path = args.output_dir / "cycle-audit.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_cycle_audit_markdown(report),
        encoding="utf-8",
    )
    print(
        f"cycles={report['cycleCount']} findings={report['findingCount']} "
        f"json={json_path} markdown={markdown_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
