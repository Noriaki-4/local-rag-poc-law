"""法令系統(法律とその政令・府省令)の対応表。

計画書 §6.3-7(IMPLEMENTSが同一法令系統の親へ接続している)と §9.1(検索範囲)に対応する。

委任先を探すとき、法令系統を指定しないと「政令」レイヤーの検索が無関係な法令系統の
施行令へ届いてしまう。親条文が属する系統内へ絞ることで、法律→政令→府省令の連鎖を
質問別の固定条番号なしに追える。
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import settings


@lru_cache(maxsize=1)
def _registry() -> dict[str, Any]:
    path: Path = settings.samples_dir / "eval" / "law_registry.json"
    if not path.exists():
        return {"laws": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"laws": []}


@lru_cache(maxsize=1)
def family_root_by_document() -> dict[str, str]:
    """documentId -> 法令系統の親法律documentId。"""
    return {
        f"law-{item['lawId']}": f"law-{item.get('familyRoot') or item['lawId']}"
        for item in _registry().get("laws", [])
        if item.get("lawId")
    }


@lru_cache(maxsize=1)
def documents_by_family_root() -> dict[str, tuple[str, ...]]:
    """法令系統の親法律documentId -> その系統に属する全documentId。"""
    grouped: dict[str, list[str]] = {}
    for document_id, family_root in family_root_by_document().items():
        grouped.setdefault(family_root, []).append(document_id)
    return {family_root: tuple(sorted(ids)) for family_root, ids in grouped.items()}


def family_root_for_article(article_id: str | None) -> str | None:
    """条IDからその法令系統の親法律を求める。registryに無い法令はNoneを返す。"""
    document_id = str(article_id or "").split("-article-", 1)[0]
    if not document_id:
        return None
    return family_root_by_document().get(document_id)


def family_document_ids(family_root: str | None) -> tuple[str, ...]:
    """同一法令系統のdocumentId群。未知の系統では空を返し、検索を絞らない。"""
    if not family_root:
        return ()
    return documents_by_family_root().get(family_root, ())


def clear_cache() -> None:
    """テストやregistry差し替え後にキャッシュを捨てる。"""
    _registry.cache_clear()
    family_root_by_document.cache_clear()
    documents_by_family_root.cache_clear()
