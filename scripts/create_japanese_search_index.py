#!/usr/bin/env python3
"""既存索引を壊さず、日本語Analyzer付きの比較索引へ再索引する。"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 600,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def _exists(url: str) -> bool:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--opensearch-url", default="http://localhost:9200")
    parser.add_argument("--source", default="legal-rag-content")
    parser.add_argument("--target", default="legal-rag-content-ja-v2")
    parser.add_argument(
        "--mapping",
        type=Path,
        default=repo_root
        / "docs/requirements/samples/metadata/"
        "opensearch_index_mapping.japanese.sample.json",
    )
    args = parser.parse_args()

    base_url = args.opensearch_url.rstrip("/")
    if not args.source or not args.target or args.source == args.target:
        parser.error("sourceとtargetには異なる具体的な索引名を指定してください")
    if not _exists(f"{base_url}/{args.source}"):
        parser.error(f"source indexが存在しません: {args.source}")
    if _exists(f"{base_url}/{args.target}"):
        parser.error(
            f"target indexは既に存在します（自動削除しません）: {args.target}"
        )

    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    create_body = {
        key: value
        for key, value in mapping.items()
        if key in {"settings", "mappings", "aliases"}
    }
    _request("PUT", f"{base_url}/{args.target}", create_body, timeout=60)
    result = _request(
        "POST",
        f"{base_url}/_reindex?refresh=true&wait_for_completion=true",
        {
            "conflicts": "abort",
            "source": {"index": args.source},
            "dest": {"index": args.target, "op_type": "create"},
        },
    )
    source_count = _request("GET", f"{base_url}/{args.source}/_count").get("count")
    target_count = _request("GET", f"{base_url}/{args.target}/_count").get("count")
    summary = {
        "source": args.source,
        "target": args.target,
        "sourceCount": source_count,
        "targetCount": target_count,
        "created": result.get("created"),
        "failures": result.get("failures") or [],
        "timedOut": bool(result.get("timed_out")),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if source_count == target_count and not summary["failures"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"OpenSearch HTTP {exc.code}: {detail}", file=sys.stderr)
        raise SystemExit(1) from exc
