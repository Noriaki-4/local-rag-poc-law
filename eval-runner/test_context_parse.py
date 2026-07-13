"""コンテキスト見出しから contentUnitId を組み立てる純関数のテスト。

seed.py の contentUnitId 生成規則（枝番は '2_12' 形式）と一致することを固定する。
e-Gov への通信を伴う _context_expected_references はここでは対象外（採点用途で end-to-end 検証済み）。
"""

import re

import run_eval as R


def test_article_suffix_matches_seed_format():
    assert R._article_suffix("第5条") == "5"
    assert R._article_suffix("第2条の12") == "2_12"
    assert R._article_suffix("第14条の2の2") == "14_2_2"
    # 全角数字も正規化される
    assert R._article_suffix("第２条の１２") == "2_12"
    # 条ではない見出しは None
    assert R._article_suffix("B 基本ガイドライン") is None
    assert R._article_suffix("第1項") is None


def test_pure_num_parses_paragraph_and_item():
    assert R._pure_num("第6項", R.PARAGRAPH_HEADER_PATTERN) == 6
    assert R._pure_num("第１号", R.ITEM_HEADER_PATTERN) == 1
    # 項パターンに号見出しは一致しない
    assert R._pure_num("第2号", R.PARAGRAPH_HEADER_PATTERN) is None
    # 条の枝番表記（第1条の5）は項・号として拾わない
    assert R._pure_num("第1条の5", R.PARAGRAPH_HEADER_PATTERN) is None


def test_add_reference_dedupes():
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    R._add_reference(refs, seen, "law-x", "law-x-article-5")
    R._add_reference(refs, seen, "law-x", "law-x-article-5")
    assert refs == [{"lawId": "law-x", "contentUnitId": "law-x-article-5"}]


def test_document_id_of_handles_suppl_ids():
    assert R._document_id_of("law-323AC0000000025-article-8-paragraph-1") == "law-323AC0000000025"
    assert R._document_id_of("law-323AC0000000025-suppl-0-article-8") == "law-323AC0000000025"


def test_family_of_maps_delegated_laws_to_parent():
    assert R._family_of("law-340CO0000000321") == "law-323AC0000000025"  # 施行令→金商法
    assert R._family_of("law-405M50000040014") == "law-323AC0000000025"  # 定義府令→金商法
    assert R._family_of("law-336M50000100001") == "law-335AC0000000145"  # 施行規則→薬機法
    assert R._family_of("law-129AC0000000089") == "law-129AC0000000089"  # 民法は自身が親
    assert R._family_of("law-unknown999") == "law-unknown999"  # 未知法令はそのまま


def test_registry_alias_resolves_context_without_egov_call(monkeypatch):
    monkeypatch.setattr(R, "_egov_title", lambda law_id: (_ for _ in ()).throw(AssertionError(law_id)))
    context = "## 定義府令\n### 第2条\n#### 第1項"

    references = R._context_expected_references(context, {"405M50000040014"})

    assert {item["contentUnitId"] for item in references} == {
        "law-405M50000040014-article-2",
        "law-405M50000040014-article-2-paragraph-1",
    }
