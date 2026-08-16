from types import SimpleNamespace

from app import legal_relation_classifier as module
from app.legal_relation_classifier import (
    ArticleText,
    LegalRelationClassificationService,
    RelationClassificationItem,
    article_texts_from_sources,
    batch_relation_items,
    validate_relation_decisions,
)


def _item(assertion_id: str = "assertion-1") -> RelationClassificationItem:
    return RelationClassificationItem(
        assertion={
            "assertionId": assertion_id,
            "fromArticleId": "law-a-article-1",
            "toArticleId": "law-b-article-2",
            "suggestedType": "IMPLEMENTS",
        },
        from_article=ArticleText("law-a-article-1", "対象は政令で定める。"),
        to_article=ArticleText("law-b-article-2", "法第一条の対象は法人とする。"),
    )


def test_decisive_verdict_requires_quotes_found_in_both_articles() -> None:
    decisions = validate_relation_decisions(
        [_item()],
        {
            "decisions": [
                {
                    "assertionId": "assertion-1",
                    "verdict": "implements",
                    "fromSupportingQuote": "存在しない引用",
                    "toSupportingQuote": "法第一条の対象",
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
                    "fromSupportingQuote": "対象は政令で定める。",
                    "toSupportingQuote": "法第一条の対象",
                    "reason": "x",
                }
            ]
        },
    )
    assert {value["verdict"] for value in decisions.values()} == {"uncertain"}


def test_batching_enforces_count_without_splitting_an_item() -> None:
    batches = batch_relation_items(
        [_item(f"a{index}") for index in range(3)],
        max_items=2,
        max_chars=100000,
    )
    assert [len(batch) for batch in batches] == [2, 1]


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
            },
            {
                "assertionId": "a2",
                "fromArticleId": "law-a-article-1",
                "toArticleId": "law-b-article-3",
                "suggestedType": "IMPLEMENTS",
            },
        ][:limit]

    def update_relation_classifications(self, records):
        self.updated = records


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
                "status": "llm_classified_implements",
                "fromArticleHash": from_article.content_hash,
                "toArticleHash": to_article.content_hash,
                "classifierPromptVersion": "legal-relation-classifier-v1",
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
        ids = schema["properties"]["decisions"]["items"]["properties"]["assertionId"][
            "enum"
        ]
        decisions = []
        for assertion_id in ids:
            if model == "haiku-test" and assertion_id == "a2":
                verdict, from_quote, to_quote = "uncertain", "", ""
            elif assertion_id == "a2":
                verdict = "reference_only"
                from_quote = "対象は政令で定める。"
                to_quote = "法第一条を参照する。"
            else:
                verdict = "implements"
                from_quote = "対象は政令で定める。"
                to_quote = "法第一条の対象は法人とする。"
            decisions.append(
                {
                    "assertionId": assertion_id,
                    "verdict": verdict,
                    "fromSupportingQuote": from_quote,
                    "toSupportingQuote": to_quote,
                    "reason": "本文による判断",
                }
            )
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

    assert llm.models == ["haiku-test", "sonnet-test"]
    assert report["verdictCounts"] == {"implements": 1, "reference_only": 1}
    assert {record["status"] for record in graph.updated} == {
        "llm_classified_implements",
        "llm_classified_reference_only",
    }
    reviewed = next(record for record in graph.updated if record["assertionId"] == "a2")
    assert reviewed["classifierReviewerModel"] == "sonnet-test"
    assert reviewed["fromArticleHash"]
    assert reviewed["classifierPromptVersion"] == "legal-relation-classifier-v1"
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
