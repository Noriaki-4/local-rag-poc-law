from types import SimpleNamespace

from app import legal_relation_classifier as module
from app.legal_relation_classifier import (
    ArticleText,
    LegalRelationClassificationService,
    RelationClassificationItem,
    article_evidence_spans,
    article_texts_from_sources,
    batch_relation_items,
    build_relation_classification_prompt,
    matching_evidence_span_ids,
    matching_evidence_span_ids_at_source_offsets,
    relation_classification_request_chars,
    relation_classification_timeout,
    relation_classification_json_schema,
    validate_relation_decisions,
)


def _item(assertion_id: str = "assertion-1") -> RelationClassificationItem:
    return RelationClassificationItem(
        assertion={
            "assertionId": assertion_id,
            "fromArticleId": "law-a-article-1",
            "toArticleId": "law-b-article-2",
            "suggestedType": "IMPLEMENTS",
            "sourceText": "法第一条の対象",
        },
        from_article=ArticleText("law-a-article-1", "対象は政令で定める。"),
        to_article=ArticleText("law-b-article-2", "法第一条の対象は法人とする。"),
    )


def test_decisive_verdict_requires_known_span_ids_from_both_articles() -> None:
    decisions = validate_relation_decisions(
        [_item()],
        {
            "decisions": [
                {
                    "assertionId": "assertion-1",
                    "verdict": "implements",
                    "delegationFinding": "explicit_same_matter",
                    "implementationFinding": "fulfills_delegation",
                    "fromSupportingSpanId": "unknown-span",
                    "toSupportingSpanId": "law-b-article-2::span-1",
                    "reason": "委任を具体化する",
                }
            ]
        },
    )
    assert decisions["assertion-1"]["verdict"] == "uncertain"


def test_missing_or_duplicate_ids_do_not_get_guessed() -> None:
    decisions = validate_relation_decisions(
        [_item("a1"), _item("a2")],
        {
            "decisions": [
                {
                    "assertionId": "unknown",
                    "verdict": "implements",
                    "delegationFinding": "explicit_same_matter",
                    "implementationFinding": "fulfills_delegation",
                    "fromSupportingSpanId": "law-a-article-1::span-1",
                    "toSupportingSpanId": "law-b-article-2::span-1",
                    "reason": "x",
                }
            ]
        },
    )
    assert {value["verdict"] for value in decisions.values()} == {"uncertain"}


def test_decisive_verdict_cannot_use_an_unrelated_to_article_span() -> None:
    item = RelationClassificationItem(
        assertion={
            "assertionId": "a1",
            "sourceText": "法第一条の対象",
        },
        from_article=ArticleText("from", "対象は政令で定める。"),
        to_article=ArticleText(
            "to", "法第一条の対象は法人とする。別件は政令で定める。"
        ),
    )

    decisions = validate_relation_decisions(
        [item],
        {
            "decisions": {
                "a1": {
                    "verdict": "implements",
                    "delegationFinding": "explicit_same_matter",
                    "implementationFinding": "fulfills_delegation",
                    "fromSupportingSpanId": "from::span-1",
                    "toSupportingSpanId": "to::span-2",
                    "reason": "別件の委任を選んだ",
                }
            }
        },
    )

    assert decisions["a1"]["verdict"] == "uncertain"


def test_batching_enforces_count_without_splitting_an_item() -> None:
    batches = batch_relation_items(
        [_item(f"a{index}") for index in range(3)],
        max_items=2,
        max_chars=100000,
    )
    assert [len(batch) for batch in batches] == [2, 1]


def test_batching_uses_serialized_request_length_including_schema() -> None:
    items = [_item("a1"), _item("a2")]
    two_item_request_chars = relation_classification_request_chars(items)

    batches = batch_relation_items(
        items,
        max_items=8,
        max_chars=two_item_request_chars - 1,
    )

    assert [len(batch) for batch in batches] == [1, 1]


def test_request_size_is_larger_than_prompt_when_schema_is_included() -> None:
    items = [_item("a1"), _item("a2")]

    assert relation_classification_request_chars(items) > len(
        build_relation_classification_prompt(items)
    )


def test_long_single_request_extends_timeout_up_to_hard_cap() -> None:
    assert relation_classification_timeout(
        30_000, base_timeout_sec=120, batch_chars=30_000
    ) == 120
    assert relation_classification_timeout(
        30_001, base_timeout_sec=120, batch_chars=30_000
    ) == 240
    assert relation_classification_timeout(
        300_000, base_timeout_sec=120, batch_chars=30_000
    ) == 600


def test_article_chunks_use_natural_paragraph_order() -> None:
    texts = article_texts_from_sources(
        ["law-a-article-1"],
        [
            {
                "contentUnitId": "law-a-article-1-paragraph-10",
                "articleContentUnitId": "law-a-article-1",
                "text": "第十項",
            },
            {
                "contentUnitId": "law-a-article-1-paragraph-2",
                "articleContentUnitId": "law-a-article-1",
                "text": "第二項",
            },
        ],
    )
    assert texts["law-a-article-1"].text == "第二項\n第十項"


def test_article_chunks_remove_only_structurally_repeated_parent_context() -> None:
    article_id = "law-a-article-1"
    paragraph_id = f"{article_id}-paragraph-1"
    texts = article_texts_from_sources(
        [article_id],
        [
            {
                "contentUnitId": paragraph_id,
                "articleContentUnitId": article_id,
                "parentContentUnitId": article_id,
                "text": "1この府令において、次のとおりとする。",
            },
            {
                "contentUnitId": f"{paragraph_id}-item-1",
                "articleContentUnitId": article_id,
                "parentContentUnitId": paragraph_id,
                "text": "この府令において、次のとおりとする。\n一　発行者",
            },
            {
                "contentUnitId": f"{paragraph_id}-item-2",
                "articleContentUnitId": article_id,
                "parentContentUnitId": paragraph_id,
                "text": "この府令において、次のとおりとする。\n二　株券等",
            },
        ],
    )

    assert texts[article_id].text == (
        "1この府令において、次のとおりとする。\n"
        "一　発行者\n"
        "二　株券等"
    )


def test_article_evidence_spans_are_stable_and_bounded() -> None:
    spans = article_evidence_spans(
        ArticleText("law-a-article-1", "第一文。第二文。\n" + "長" * 401)
    )

    assert spans == {
        "law-a-article-1::span-1": "第一文。",
        "law-a-article-1::span-2": "第二文。",
        "law-a-article-1::span-3": "長" * 400,
        "law-a-article-1::span-4": "長",
    }


def test_reference_occurrence_can_map_across_span_boundary() -> None:
    spans = {"span-1": "委任事項の前", "span-2": "半と後半を定める。"}

    assert matching_evidence_span_ids("前半と後半", spans) == [
        "span-1",
        "span-2",
    ]


def test_reference_occurrence_maps_every_repeated_location() -> None:
    spans = {
        "to::span-1": "法第一条を参照する。",
        "to::span-2": "別の説明。法第一条を具体化する。",
    }

    assert matching_evidence_span_ids("法第一条", spans) == [
        "to::span-1",
        "to::span-2",
    ]


def test_reference_occurrence_offsets_select_only_the_intended_repetition() -> None:
    spans = {
        "article::span-1": "第九十八条の規定により通知する。",
        "article::span-2": "第九十八条の規定にかかわらず変更する。",
    }
    source_text = "第九十八条の規定にかかわらず変更する。"

    assert matching_evidence_span_ids_at_source_offsets(
        "第九十八条",
        spans,
        source_text=source_text,
        source_start=0,
        source_end=len("第九十八条"),
    ) == ["article::span-2"]


def test_decisive_verdict_requires_reference_occurrence_mapping() -> None:
    item = _item()
    item = RelationClassificationItem(
        assertion={**item.assertion, "sourceText": "本文に存在しない参照"},
        from_article=item.from_article,
        to_article=item.to_article,
    )

    decisions = validate_relation_decisions(
        [item],
        {
            "decisions": {
                "assertion-1": {
                    "verdict": "implements",
                    "delegationFinding": "explicit_same_matter",
                    "implementationFinding": "fulfills_delegation",
                    "fromSupportingSpanId": "law-a-article-1::span-1",
                    "toSupportingSpanId": "law-b-article-2::span-1",
                    "reason": "委任を具体化する",
                }
            }
        },
    )

    assert decisions["assertion-1"]["verdict"] == "uncertain"


def test_v8_schema_uses_assertion_ids_as_required_decision_keys() -> None:
    schema = relation_classification_json_schema(["a1", "a2"])
    decisions = schema["properties"]["decisions"]

    assert decisions["type"] == "object"
    assert decisions["required"] == ["a1", "a2"]
    assert decisions["additionalProperties"] is False
    assert set(decisions["properties"]) == {"a1", "a2"}
    decision = decisions["properties"]["a1"]
    assert "delegationFinding" in decision["required"]
    assert "implementationFinding" in decision["required"]
    assert "fromSupportingSpanId" in decision["required"]
    assert "toSupportingSpanId" in decision["required"]


def test_prompt_binds_decision_to_all_candidate_reference_occurrences() -> None:
    item = RelationClassificationItem(
        assertion={
            "assertionId": "a1",
            "fromArticleId": "law-a-article-1",
            "toArticleId": "law-b-article-2",
            "suggestedType": "IMPLEMENTS",
            "sourceText": "法第一条の対象",
            "sourceTexts": ["法第一条の対象", "法第一条に基づく"],
        },
        from_article=ArticleText("law-a-article-1", "対象は政令で定める。"),
        to_article=ArticleText(
            "law-b-article-2",
            "法第一条の対象は法人とする。法第一条に基づく。",
        ),
    )

    prompt = build_relation_classification_prompt([item])

    assert '"text": "法第一条の対象"' in prompt
    assert '"text": "法第一条に基づく"' in prompt
    assert prompt.count('"articleId": "law-b-article-2"') == 3
    assert (
        '"matchingToSpanIds": ["law-b-article-2::span-1"]' in prompt
    )
    assert '"fromArticle": {"articleId": "law-a-article-1"' in prompt
    assert '"toArticle": {"articleId": "law-b-article-2"' in prompt
    assert '"law-a-article-1::span-1":' in prompt
    assert '"law-b-article-2::span-2":' in prompt
    assert '"suggestedType"' not in prompt
    assert '"candidateSource"' not in prompt
    assert "少なくとも一つ" in prompt
    assert "同じArticle内の別の参照・委任へ" in prompt
    assert "判断対象を移さない" in prompt


def test_each_candidate_contains_its_own_article_context() -> None:
    prompt = build_relation_classification_prompt([_item("a1"), _item("a2")])

    assert prompt.count('"fromArticle":') == 2
    assert prompt.count('"toArticle":') == 2
    assert "Article本文（候補のfromArticleId" not in prompt


class _Graph:
    def __init__(self) -> None:
        self.updated = []

    def relation_assertions_for_classification(self, *, limit=None):
        return [
            {
                "assertionId": "a1",
                "fromArticleId": "law-a-article-1",
                "toArticleId": "law-b-article-2",
                "suggestedType": "IMPLEMENTS",
                "sourceText": "法第一条の対象",
            },
            {
                "assertionId": "a2",
                "fromArticleId": "law-a-article-1",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
                "sourceText": "法第一条",
            },
        ][:limit]

    def update_relation_classifications(self, records):
        self.updated.extend(records)


class _CurrentGraph(_Graph):
    def __init__(self, classifier_model: str) -> None:
        super().__init__()
        self.classifier_model = classifier_model

    def relation_assertions_for_classification(self, *, limit=None):
        from_article = ArticleText("law-a-article-1", "対象は政令で定める。")
        to_article = ArticleText("law-b-article-2", "法第一条の対象は法人とする。")
        return [
            {
                "assertionId": "a1",
                "fromArticleId": from_article.article_id,
                "toArticleId": to_article.article_id,
                "suggestedType": "IMPLEMENTS",
                "sourceText": "法第一条の対象",
                "status": "llm_classified_implements",
                "fromArticleHash": from_article.content_hash,
                "toArticleHash": to_article.content_hash,
                "classifierPromptVersion": "legal-relation-classifier-v8",
                "classifierModel": self.classifier_model,
                "classifierReviewerModel": "",
            }
        ]


class _OpenSearch:
    def get_complete_articles_by_ids(self, article_ids, user_clearance_level):
        texts = {
            "law-a-article-1": "対象は政令で定める。",
            "law-b-article-2": "法第一条の対象は法人とする。",
            "law-b-article-3": "法第一条を参照する。",
        }
        return [
            {
                "contentUnitId": article_id,
                "articleContentUnitId": article_id,
                "text": texts[article_id],
            }
            for article_id in article_ids
        ]


class _LLM:
    provider = "anthropic"

    def __init__(self) -> None:
        self.models = []

    def generate_structured_json(self, *, prompt, schema, model, **kwargs):
        self.models.append(model)
        ids = schema["properties"]["decisions"]["required"]
        decisions = {}
        for assertion_id in ids:
            if model == "haiku-test" and assertion_id == "a2":
                verdict, from_span_id, to_span_id = "uncertain", "", ""
                delegation_finding = "uncertain"
                implementation_finding = "uncertain"
            elif assertion_id == "a2":
                verdict = "reference_only"
                from_span_id = "law-a-article-1::span-1"
                to_span_id = "law-b-article-3::span-1"
                delegation_finding = "not_explicit_same_matter"
                implementation_finding = "does_not_fulfill_delegation"
            else:
                verdict = "implements"
                from_span_id = "law-a-article-1::span-1"
                to_span_id = "law-b-article-2::span-1"
                delegation_finding = "explicit_same_matter"
                implementation_finding = "fulfills_delegation"
            decisions[assertion_id] = {
                "verdict": verdict,
                "delegationFinding": delegation_finding,
                "implementationFinding": implementation_finding,
                "fromSupportingSpanId": from_span_id,
                "toSupportingSpanId": to_span_id,
                "reason": "本文による判断",
            }
        return SimpleNamespace(payload={"decisions": decisions})


def test_service_reviews_only_primary_uncertain_and_persists_provenance(
    monkeypatch,
) -> None:
    monkeypatch.setattr(module.settings, "relation_classifier_model", "haiku-test")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "sonnet-test"
    )
    graph = _Graph()
    llm = _LLM()
    report = LegalRelationClassificationService(graph, _OpenSearch(), llm).run()

    assert llm.models == ["haiku-test", "haiku-test", "sonnet-test"]
    assert report["verdictCounts"] == {"implements": 1, "reference_only": 1}
    assert {record["status"] for record in graph.updated} == {
        "llm_classified_implements",
        "llm_classified_reference_only",
    }
    reviewed = next(record for record in graph.updated if record["assertionId"] == "a2")
    assert reviewed["classifierReviewerModel"] == "sonnet-test"
    assert reviewed["fromArticleHash"]
    assert reviewed["classifierPromptVersion"] == "legal-relation-classifier-v8"
    assert reviewed["classifierPromptHash"]


def test_changing_primary_model_invalidates_cached_classification(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "relation_classifier_model", "haiku-new")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "sonnet-test"
    )
    graph = _CurrentGraph("haiku-old")
    llm = _LLM()

    report = LegalRelationClassificationService(graph, _OpenSearch(), llm).run()

    assert report["classifiedCount"] == 1
    assert llm.models == ["haiku-new"]


def test_matching_hash_prompt_and_model_skip_api_call(monkeypatch) -> None:
    monkeypatch.setattr(module.settings, "relation_classifier_model", "haiku-test")
    monkeypatch.setattr(
        module.settings, "relation_classifier_reviewer_model", "sonnet-test"
    )
    graph = _CurrentGraph("haiku-test")
    llm = _LLM()

    report = LegalRelationClassificationService(graph, _OpenSearch(), llm).run()

    assert report["skippedCurrentCount"] == 1
    assert llm.models == []
    assert graph.updated == []
