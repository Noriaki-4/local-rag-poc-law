from app.evidence_selector import (
    AspectEvidence,
    AspectEvidenceMatrix,
    article_id,
    aspect_queries_by_article,
    select_issue_covered_context,
)


def _item(
    content_unit_id: str,
    *,
    article_content_unit_id: str | None = None,
    doc_type: str = "law",
    sources: list[str] | None = None,
) -> dict:
    document_id = content_unit_id.split("-article-", 1)[0]
    return {
        "document": {
            "documentId": document_id,
            "contentUnitId": content_unit_id,
            "articleContentUnitId": article_content_unit_id,
            "docType": doc_type,
            "text": content_unit_id,
        },
        "score": 0.1,
        "sources": sources or ["initial_search"],
        "introducedBy": (sources or ["initial_search"])[0],
    }


def _matrix(*orders: tuple[str, list[str]], used: bool = True) -> AspectEvidenceMatrix:
    return AspectEvidenceMatrix(
        [
            AspectEvidence(
                query=query,
                searched_content_ids=list(content_ids),
                ordered_content_ids=list(content_ids),
                used=used,
            )
            for query, content_ids in orders
        ]
    )


def test_selector_rescues_an_aspect_candidate_ranked_eighteenth():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    target_id = "law-test-article-18"
    matrix = _matrix(("50名基準", [target_id, "law-test-article-2"]))

    result = select_issue_covered_context(ranked, matrix, top_k=16)

    assert target_id in [item["document"]["contentUnitId"] for item in result.items]
    assert target_id in result.aspect_protected_ids
    assert len(result.global_rank_ids) >= 8


def test_protected_budget_counts_chunks_not_articles_and_keeps_eight_global():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    same_article = "law-test-article-23_2_17"
    explicit = [
        _item(
            f"{same_article}-paragraph-{paragraph}",
            article_content_unit_id=same_article,
            sources=["article_reference"],
        )
        for paragraph in (1, 3, 4)
    ]
    ranked[-3:] = explicit
    aspect_orders = [
        (f"論点{index}", [f"law-test-article-{20 + index}"])
        for index in range(1, 5)
    ]

    result = select_issue_covered_context(
        ranked,
        _matrix(*aspect_orders),
        top_k=16,
    )

    assert len(result.explicit_protected_ids) == 3
    assert len(result.protected_ids) <= 8
    assert len(result.global_rank_ids) >= 8
    assert len(result.items) == 16
    assert len({article_id(item) for item in explicit}) == 1


def test_explicit_overflow_can_still_enter_through_an_aspect_slot():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    explicit_ids = [f"law-test-article-{index}" for index in range(21, 26)]
    for index, item_id in enumerate(explicit_ids, start=20):
        ranked[index]["sources"].append("article_reference")
    overflow_id = explicit_ids[-1]
    matrix = _matrix(("超過明示条文の論点", [overflow_id, "law-test-article-1"]))

    result = select_issue_covered_context(ranked, matrix, top_k=16)

    assert overflow_id not in result.explicit_protected_ids
    assert overflow_id in result.aspect_protected_ids


def test_shared_top_article_covers_two_aspects_without_spending_two_slots():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    shared = "law-test-article-20"
    matrix = _matrix(
        ("論点A", [shared, "law-test-article-21"]),
        ("論点B", [shared, "law-test-article-22"]),
    )

    result = select_issue_covered_context(
        ranked,
        matrix,
        top_k=16,
        rounds=1,
    )

    assert result.aspect_protected_ids == [shared]
    assert result.covered_articles_by_query == {
        "論点A": [shared],
        "論点B": [shared],
    }


def test_guideline_does_not_fill_an_aspect_law_slot():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 29)]
    guideline = _item("guidance-test-chunk-1", doc_type="guideline")
    law = _item("law-test-article-30")
    ranked.extend([guideline, law])
    matrix = _matrix(("法令根拠", ["guidance-test-chunk-1", "law-test-article-30"]))

    result = select_issue_covered_context(ranked, matrix, top_k=16)

    assert result.aspect_protected_ids == ["law-test-article-30"]


def test_failed_aspect_does_not_force_rrf_candidates():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    target = "law-test-article-20"
    matrix = _matrix(("失敗論点", [target]), used=False)

    result = select_issue_covered_context(ranked, matrix, top_k=16)

    assert target not in [item["document"]["contentUnitId"] for item in result.items]
    assert result.aspect_protected_ids == []


def test_at_most_first_four_aspects_are_used():
    ranked = [_item(f"law-test-article-{index}") for index in range(1, 31)]
    matrix = _matrix(
        *((f"論点{index}", [f"law-test-article-{20 + index}"]) for index in range(1, 6))
    )

    result = select_issue_covered_context(ranked, matrix, top_k=16, max_aspects=4)

    assert "law-test-article-25" not in result.aspect_protected_ids


def test_article_aspect_mapping_does_not_depend_on_legacy_evidence_flags():
    evidence = {
        "law-test-article-20": _item("law-test-article-20"),
        "law-test-article-21": _item("law-test-article-21"),
    }
    matrix = _matrix(("委任論点", ["law-test-article-20", "law-test-article-21"]))

    mapping = aspect_queries_by_article(matrix, evidence, per_query=1)

    assert mapping == {"law-test-article-20": {"委任論点"}}
    assert all("aspectQueries" not in item for item in evidence.values())


def test_article_id_normalizes_item_chunk_without_article_metadata():
    item = _item("law-test-article-20-item-3")
    item["document"]["articleContentUnitId"] = None

    assert article_id(item) == "law-test-article-20"
