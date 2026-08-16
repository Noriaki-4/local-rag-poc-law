from types import SimpleNamespace

import pytest

from app.llm_directed_research import (
    EvidenceCatalog,
    ResearchAction,
    ResearchCheckpoint,
    ResearchHypothesis,
    ResearchLogicalStructure,
    ResearchRelationDecision,
    ResearchTurn,
)
from app.research_case_store import (
    TASK_CANDIDATE,
    TASK_COMPLETED,
    TASK_PENDING,
    InMemoryCaseStore,
)


def _source(
    article_id: str,
    content_unit_id: str,
    *,
    title: str = "テスト法",
    heading: str = "第一条",
) -> dict:
    return {
        "articleContentUnitId": article_id,
        "contentUnitId": content_unit_id,
        "documentId": article_id.split("-article-")[0],
        "docType": "law",
        "title": title,
        "heading": heading,
        "text": "確認済みの条文本文。",
    }


def _execution(**overrides):
    values = {
        "result_count": 1,
        "new_evidence_count": 1,
        "new_article_count": 1,
        "error": None,
        "returned_content_unit_ids": (
            "law-a-article-1-paragraph-1",
        ),
        "new_content_unit_ids": (
            "law-a-article-1-paragraph-1",
        ),
        "new_article_ids": ("law-b-article-2",),
        "auto_graph_article_ids": ("law-b-article-2",),
        "graph_relations": (
            {
                "fromArticleId": "law-a-article-1",
                "edgeType": "IMPLEMENTS",
                "toArticleId": "law-b-article-2",
                "toTitle": "テスト府令",
                "toHeading": "第二条",
            },
        ),
        "relation_assertions": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_graph_discovery_is_committed_as_candidate_task_before_checkpoint() -> None:
    case = InMemoryCaseStore().create_case("府令の具体化規定は何か")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _source(
                "law-a-article-1",
                "law-a-article-1-paragraph-1",
            )
        ]
    )
    action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
        reason="起点条文を読む",
    )

    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(),
        catalog=catalog,
    )
    assert case.latest_checkpoint is None
    assert case.tasks[task.task_ref].status == TASK_COMPLETED
    candidates = [
        item
        for item in case.tasks.values()
        if item.target_article_id == "law-b-article-2"
    ]
    assert len(candidates) == 1
    assert candidates[0].status == TASK_CANDIDATE
    context = case.llm_input_context()
    assert "law-b-article-2" in context["allowedArticleIds"]
    assert any(
        item["targetArticleId"] == "law-b-article-2"
        for item in context["candidateTasks"]
    )


def test_offline_classification_is_not_stored_as_verified_graph_fact() -> None:
    case = InMemoryCaseStore().create_case("分類済み関係を探索に使う")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [_source("law-a-article-1", "law-a-article-1-paragraph-1")]
    )
    action = ResearchAction(
        tool="expand_graph",
        articleIds=["law-a-article-1"],
        edgeTypes=["IMPLEMENTS"],
    )
    task = case.register_action(action, phase="explore")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            graph_relations=(
                {
                    "fromArticleId": "law-a-article-1",
                    "edgeType": "IMPLEMENTS",
                    "toArticleId": "law-b-article-2",
                    "relationSource": "offline_llm_classification",
                },
            ),
        ),
        catalog=catalog,
    )

    stored = case.relations[
        ("law-a-article-1", "IMPLEMENTS", "law-b-article-2")
    ]
    assert stored["status"] == "preclassified_navigation"


def test_search_discovery_is_kept_as_fetch_candidate_for_later_turns() -> None:
    case = InMemoryCaseStore().create_case("検索で見つけた条文を後で読む")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [_source("law-b-article-2", "law-b-article-2-paragraph-1")]
    )
    action = ResearchAction(
        tool="search_corpus",
        query="届出を要しない募集",
        documentIds=["law-b"],
    )
    task = case.register_action(action, phase="explore")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=("law-b-article-2-paragraph-1",),
            new_article_ids=("law-b-article-2",),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    context = case.llm_input_context()

    candidate = next(
        item
        for item in context["candidateTasks"]
        if item["targetArticleId"] == "law-b-article-2"
    )
    assert candidate["origin"] == "search_result"
    assert candidate["heading"] == "第一条"
    assert "law-b-article-2" in context["allowedArticleIds"]


def test_hypothesis_is_linked_from_search_through_candidate_and_result() -> None:
    case = InMemoryCaseStore().create_case("許可義務の仮説を検証する")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [_source("law-b-article-2", "law-b-article-2-paragraph-1")]
    )
    hypothesis = ResearchHypothesis(
        hypothesisId="H-permit",
        statement="許可を受ける必要がある",
        missing=["義務の本則"],
    )
    case.record_stage_decision(
        ResearchTurn(
            status="continue",
            hypotheses=[hypothesis],
            actions=[],
        ),
        phase="explore",
    )
    action = ResearchAction(
        tool="search_corpus",
        query="許可を受けなければならない",
        hypothesisIds=["H-permit"],
    )
    task = case.register_action(action, phase="explore")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=("law-b-article-2-paragraph-1",),
            new_article_ids=("law-b-article-2",),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    context = case.llm_input_context()
    stored = context["hypotheses"][0]
    candidate = next(
        item
        for item in context["candidateTasks"]
        if item["targetArticleId"] == "law-b-article-2"
    )
    assert stored["hypothesisId"] == "H-permit"
    assert stored["status"] == "unverified"
    assert task.task_ref in stored["testTaskRefs"]
    assert "law-b-article-2-paragraph-1" in stored["observedEvidenceRefs"]
    assert candidate["hypothesisIds"] == ["H-permit"]


def test_candidate_task_view_rotates_across_large_document_results() -> None:
    case = InMemoryCaseStore().create_case("多数候補を順に確認する")
    catalog = EvidenceCatalog()
    article_ids = [f"law-b-article-{index}" for index in range(1, 13)]
    for article_id in article_ids:
        catalog.add_results(
            [_source(article_id, f"{article_id}-paragraph-1")]
        )
    action = ResearchAction(tool="search_corpus", query="具体的要件")
    task = case.register_action(action, phase="explore")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=tuple(
                f"{article_id}-paragraph-1" for article_id in article_ids
            ),
            new_article_ids=tuple(article_ids),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    first = case.llm_input_context(max_candidate_tasks=4)
    second = case.llm_input_context(max_candidate_tasks=4)
    third = case.llm_input_context(max_candidate_tasks=4)

    pages = [
        [item["targetArticleId"] for item in context["candidateTasks"]]
        for context in (first, second, third)
    ]
    assert len({article_id for page in pages for article_id in page}) == 12
    assert all(len(page) == 4 for page in pages)


def test_candidate_task_view_does_not_advance_for_integration_snapshot() -> None:
    case = InMemoryCaseStore().create_case("統合表示では候補ページを消費しない")
    catalog = EvidenceCatalog()
    article_ids = [f"law-b-article-{index}" for index in range(1, 9)]
    for article_id in article_ids:
        catalog.add_results(
            [_source(article_id, f"{article_id}-paragraph-1")]
        )
    action = ResearchAction(tool="search_corpus", query="具体的要件")
    task = case.register_action(action, phase="explore")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=tuple(
                f"{article_id}-paragraph-1" for article_id in article_ids
            ),
            new_article_ids=tuple(article_ids),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    first = case.llm_input_context(max_candidate_tasks=4)
    integration = case.llm_input_context(
        max_candidate_tasks=4,
        advance_candidate_cursor=False,
    )
    next_stage = case.llm_input_context(max_candidate_tasks=4)

    first_ids = {
        item["targetArticleId"] for item in first["candidateTasks"]
    }
    integration_ids = {
        item["targetArticleId"] for item in integration["candidateTasks"]
    }
    next_stage_ids = {
        item["targetArticleId"] for item in next_stage["candidateTasks"]
    }
    assert integration_ids == next_stage_ids
    assert first_ids.isdisjoint(next_stage_ids)


def test_checkpoint_promotes_graph_candidate_without_copying_task_state() -> None:
    case = InMemoryCaseStore().create_case("府令の具体化規定は何か")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _source(
                "law-a-article-1",
                "law-a-article-1-paragraph-1",
            )
        ]
    )
    action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(),
        catalog=catalog,
    )

    record = case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            conclusion="府令本文の確認が必要",
            nextArticleIds=["law-b-article-2"],
        )
    )

    candidate = next(
        item
        for item in case.tasks.values()
        if item.target_article_id == "law-b-article-2"
    )
    assert candidate.status == TASK_PENDING
    assert candidate.task_ref in record.pending_task_refs
    assert record.store_version == case.current_version
    assert case.latest_checkpoint is record


def test_checkpoint_does_not_infer_task_from_free_text_missing() -> None:
    case = InMemoryCaseStore().create_case("譲渡制限付株式の要件")
    catalog = EvidenceCatalog()
    candidates = [
        _source(
            "law-decree-article-2_12",
            "law-decree-article-2_12-paragraph-1",
            title="金融商品取引法施行令",
            heading="第二条の十二（譲渡制限付株式）",
        ),
        _source(
            "law-decree-article-2_12_2",
            "law-decree-article-2_12_2-paragraph-1",
            title="金融商品取引法施行令",
            heading="第二条の十二の二",
        ),
    ]
    catalog.add_results(candidates)
    case.record_stage_decision(
        ResearchTurn(
            status="continue",
            hypotheses=[
                ResearchHypothesis(
                    hypothesisId="H-stock",
                    statement="譲渡制限付株式に該当する",
                    status="partially_supported",
                    evidenceIds=["known-evidence"],
                    missing=[
                        "金融商品取引法施行令第2条の12本文"
                    ],
                )
            ],
        ),
        phase="deepen",
    )
    action = ResearchAction(
        tool="search_corpus",
        query="譲渡制限付株式",
        hypothesisIds=["H-stock"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=tuple(
                item["contentUnitId"] for item in candidates
            ),
            new_article_ids=(
                "law-decree-article-2_12",
                "law-decree-article-2_12_2",
            ),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )
    wrong_document = case._new_task(
        task_type="fetch_articles",
        status=TASK_CANDIDATE,
        origin="search_result",
        purpose="candidate",
        article_ids=("law-act-article-2_12",),
        target_article_id="law-act-article-2_12",
        hypothesis_ids=("H-stock",),
        title="金融商品取引法",
        heading="第二条の十二",
        priority=50,
    )
    supplementary = case._new_task(
        task_type="fetch_articles",
        status=TASK_CANDIDATE,
        origin="search_result",
        purpose="candidate",
        article_ids=("law-decree-suppl-1-article-2_12",),
        target_article_id="law-decree-suppl-1-article-2_12",
        hypothesis_ids=("H-stock",),
        title="金融商品取引法施行令",
        heading="附則 第二条の十二",
        priority=50,
    )

    record = case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            logicalStructure={
                "hypotheses": [
                    {
                        "hypothesisId": "H-stock",
                        "statement": "譲渡制限付株式に該当する",
                        "status": "partially_supported",
                        "evidenceIds": ["known-evidence"],
                        "missing": [
                            "金融商品取引法施行令第2条の12本文"
                        ],
                    }
                ]
            },
        )
    )

    exact = next(
        item
        for item in case.tasks.values()
        if item.target_article_id == "law-decree-article-2_12"
    )
    branch = next(
        item
        for item in case.tasks.values()
        if item.target_article_id == "law-decree-article-2_12_2"
    )
    assert exact.status == TASK_CANDIDATE
    assert branch.status == TASK_CANDIDATE
    assert wrong_document.status == TASK_CANDIDATE
    assert supplementary.status == TASK_CANDIDATE
    assert record.pending_task_refs == ()


def test_checkpoint_does_not_infer_task_from_heading_similarity() -> None:
    case = InMemoryCaseStore().create_case("公開買付開始公告の手続")
    catalog = EvidenceCatalog()
    candidate = _source(
        "law-ordinance-article-10",
        "law-ordinance-article-10-paragraph-1",
        title="公開買付府令",
        heading="第十条（公開買付開始公告）",
    )
    catalog.add_results([candidate])
    case.record_stage_decision(
        ResearchTurn(
            status="continue",
            hypotheses=[
                ResearchHypothesis(
                    hypothesisId="H-notice",
                    statement="公開買付開始公告の手続が必要",
                    status="supported",
                    evidenceIds=["known-evidence"],
                )
            ],
        ),
        phase="deepen",
    )
    action = ResearchAction(
        tool="search_corpus",
        query="公開買付開始公告",
        hypothesisIds=["H-notice"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=(candidate["contentUnitId"],),
            new_article_ids=(candidate["articleContentUnitId"],),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            logicalStructure={
                "hypotheses": [
                    {
                        "hypothesisId": "H-notice",
                        "statement": "公開買付開始公告の手続が必要",
                        "status": "supported",
                        "evidenceIds": ["known-evidence"],
                    }
                ]
            },
        )
    )

    candidate_task = next(
        item
        for item in case.tasks.values()
        if item.target_article_id == "law-ordinance-article-10"
    )
    assert candidate_task.status == TASK_CANDIDATE


def test_checkpoint_does_not_promote_weakly_related_candidate() -> None:
    case = InMemoryCaseStore().create_case("公開買付の手続")
    case.hypotheses["H-procedure"] = {
        "hypothesisId": "H-procedure",
        "statement": "公開買付の手続を確認する",
        "status": "unverified",
        "evidenceIds": [],
        "missing": [],
    }
    candidate = case._new_task(
        task_type="fetch_articles",
        status=TASK_CANDIDATE,
        origin="search_result",
        purpose="candidate",
        article_ids=("law-ordinance-article-99",),
        target_article_id="law-ordinance-article-99",
        hypothesis_ids=("H-procedure",),
        title="公開買付府令",
        heading="第九十九条（雑則）",
        priority=50,
    )

    case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            logicalStructure={
                "hypotheses": [case.hypotheses["H-procedure"]]
            },
        )
    )

    assert candidate.status == TASK_CANDIDATE


def test_relation_assertion_stays_candidate_until_llm_checkpoint_decision() -> None:
    case = InMemoryCaseStore().create_case("委任関係を本文で確認する")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _source(
                "law-a-article-1",
                "law-a-article-1-paragraph-1",
            ),
            _source(
                "law-b-article-2",
                "law-b-article-2-paragraph-1",
                title="テスト施行令",
                heading="第二条",
            ),
        ]
    )
    assertion = {
        "assertionId": "assertion-1",
        "fromArticleId": "law-a-article-1",
        "toArticleId": "law-b-article-2",
        "suggestedType": "IMPLEMENTS",
        "status": "unverified",
    }
    catalog.add_relation_assertions([assertion])
    action = ResearchAction(
        tool="expand_graph",
        articleIds=["law-a-article-1"],
        edgeTypes=["IMPLEMENTS"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=(),
            new_article_ids=(),
            auto_graph_article_ids=(),
            graph_relations=(),
            relation_assertions=(assertion,),
        ),
        catalog=catalog,
    )

    assert case.relations == {}
    assert case.relation_assertions["assertion-1"]["status"] == "unverified"
    assert case.relation_decisions == {}

    case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            logicalStructure=ResearchLogicalStructure(
                relationDecisions=[
                    ResearchRelationDecision(
                        assertionId="assertion-1",
                        verdict="confirmed",
                        relationType="IMPLEMENTS",
                        fromArticleId="law-a-article-1",
                        toArticleId="law-b-article-2",
                        evidenceIds=[
                            "law-a-article-1-paragraph-1",
                            "law-b-article-2-paragraph-1",
                        ],
                        reason="両条文本文から具体化関係を確認",
                    )
                ]
            ),
        )
    )

    assert case.relation_decisions["assertion-1"]["verdict"] == "confirmed"
    assert case.llm_input_context()["relationCandidates"] == []


def test_case_allows_only_one_running_task() -> None:
    case = InMemoryCaseStore().create_case("二つの条文を調査する")
    first = case.register_action(
        ResearchAction(
            tool="search_corpus",
            query="第一の検索",
        ),
        phase="explore",
    )
    second = case.register_action(
        ResearchAction(
            tool="search_corpus",
            query="第二の検索",
        ),
        phase="explore",
    )

    case.start_task(first.task_ref)
    with pytest.raises(RuntimeError, match="only one research task"):
        case.start_task(second.task_ref)


def test_batch_fetch_completes_existing_article_candidate_tasks() -> None:
    case = InMemoryCaseStore().create_case("関連する二つの条文を読む")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [
            _source("law-a-article-1", "law-a-article-1-paragraph-1"),
            _source("law-b-article-2", "law-b-article-2-paragraph-1"),
            _source("law-c-article-3", "law-c-article-3-paragraph-1"),
        ]
    )
    root_action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
    )
    root_task = case.register_action(root_action, phase="deepen")
    case.start_task(root_task.task_ref)
    case.complete_tool_task(
        task_ref=root_task.task_ref,
        action=root_action,
        execution=_execution(),
        catalog=catalog,
    )
    candidate = next(
        task
        for task in case.tasks.values()
        if task.target_article_id == "law-b-article-2"
    )
    assert candidate.status == TASK_CANDIDATE

    batch_action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-b-article-2", "law-c-article-3"],
    )
    batch_task = case.register_action(batch_action, phase="deepen")
    case.start_task(batch_task.task_ref)
    case.complete_tool_task(
        task_ref=batch_task.task_ref,
        action=batch_action,
        execution=_execution(
            returned_content_unit_ids=(
                "law-b-article-2-paragraph-1",
                "law-c-article-3-paragraph-1",
            ),
            new_content_unit_ids=(
                "law-b-article-2-paragraph-1",
                "law-c-article-3-paragraph-1",
            ),
            new_article_ids=("law-b-article-2", "law-c-article-3"),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    assert candidate.status == TASK_COMPLETED
    assert not any(
        task.status in {TASK_CANDIDATE, TASK_PENDING}
        and "law-b-article-2" in task.article_ids
        for task in case.tasks.values()
    )


def test_checkpoint_does_not_requeue_article_whose_body_is_fetched() -> None:
    case = InMemoryCaseStore().create_case("取得済み条文を再登録しない")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [_source("law-a-article-1", "law-a-article-1-paragraph-1")]
    )
    action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            returned_content_unit_ids=("law-a-article-1-paragraph-1",),
            new_content_unit_ids=("law-a-article-1-paragraph-1",),
            new_article_ids=("law-a-article-1",),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    record = case.create_checkpoint(
        ResearchCheckpoint(
            status="continue",
            nextArticleIds=["law-a-article-1"],
        )
    )

    assert record.pending_task_refs == ()
    assert case.runnable_tasks() == ()


def test_candidate_view_keeps_one_candidate_from_each_target_document() -> None:
    case = InMemoryCaseStore().create_case("複数の配下法令を確認する")
    catalog = EvidenceCatalog()
    catalog.add_results(
        [_source("law-a-article-1", "law-a-article-1-paragraph-1")]
    )
    relations = tuple(
        {
            "fromArticleId": "law-a-article-1",
            "edgeType": "IMPLEMENTS",
            "toArticleId": f"law-b-article-{index}",
            "toDocumentId": "law-b",
            "toTitle": "同一府令",
            "toHeading": f"第{index}条",
        }
        for index in range(1, 41)
    ) + (
        {
            "fromArticleId": "law-a-article-1",
            "edgeType": "IMPLEMENTS",
            "toArticleId": "law-z-article-13",
            "toDocumentId": "law-z",
            "toTitle": "定義府令",
            "toHeading": "第十三条",
        },
    )
    action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
    )
    task = case.register_action(action, phase="deepen")
    case.start_task(task.task_ref)
    case.complete_tool_task(
        task_ref=task.task_ref,
        action=action,
        execution=_execution(
            new_article_ids=tuple(
                relation["toArticleId"] for relation in relations
            ),
            auto_graph_article_ids=(),
            graph_relations=relations,
        ),
        catalog=catalog,
    )

    context = case.llm_input_context(max_candidate_tasks=32)

    assert len(context["candidateTasks"]) == 32
    assert any(
        task["targetArticleId"] == "law-z-article-13"
        for task in context["candidateTasks"]
    )
    assert context["omittedCandidateTaskCount"] == 9


def test_graph_candidate_alone_does_not_block_ready() -> None:
    case = InMemoryCaseStore().create_case("配下法令の本文も確認する")
    catalog = EvidenceCatalog()
    source = _source("law-a-article-1", "law-a-article-1-paragraph-1")
    target = _source("law-b-article-2", "law-b-article-2-paragraph-1")
    catalog.add_results([source, target])

    search_action = ResearchAction(tool="search_corpus", query="具体的要件")
    search_task = case.register_action(search_action, phase="explore")
    case.start_task(search_task.task_ref)
    case.complete_tool_task(
        task_ref=search_task.task_ref,
        action=search_action,
        execution=_execution(
            returned_content_unit_ids=(),
            new_content_unit_ids=("law-b-article-2-paragraph-1",),
            new_article_ids=("law-b-article-2",),
            auto_graph_article_ids=(),
            graph_relations=(),
        ),
        catalog=catalog,
    )

    fetch_action = ResearchAction(
        tool="fetch_articles",
        articleIds=["law-a-article-1"],
    )
    fetch_task = case.register_action(fetch_action, phase="deepen")
    case.start_task(fetch_task.task_ref)
    case.complete_tool_task(
        task_ref=fetch_task.task_ref,
        action=fetch_action,
        execution=_execution(
            returned_content_unit_ids=("law-a-article-1-paragraph-1",),
            new_content_unit_ids=("law-a-article-1-paragraph-1",),
            new_article_ids=(),
            auto_graph_article_ids=(),
            graph_relations=(
                {
                    "fromArticleId": "law-a-article-1",
                    "edgeType": "IMPLEMENTS",
                    "toArticleId": "law-b-article-2",
                    "toDocumentId": "law-b",
                    "toTitle": "テスト府令",
                    "toHeading": "第二条",
                },
            ),
        ),
        catalog=catalog,
    )

    checkpoint = ResearchCheckpoint(
        status="ready",
        evidenceIds=["law-a-article-1-paragraph-1"],
    )

    assert case.ready_blocking_article_ids(checkpoint) == ()

    explicit_checkpoint = checkpoint.model_copy(
        update={"nextArticleIds": ["law-b-article-2"]}
    )
    assert case.ready_blocking_article_ids(explicit_checkpoint) == (
        "law-b-article-2",
    )
